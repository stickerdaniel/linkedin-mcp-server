"""Calls racing the owner's retirement: the lines read, the stand-down, the
verdicts and the rows' wiring.

No browser. The **lines** the rows look for are produced by the product's
own code, the frontend's middleware and the owner's serving loop, and read
back through the rows' parsers. The **stand-down** goes through the harness's
sender against a recording transport. The **verdicts** start from an explicit
valid record of each row and change one observation at a time. The
**wiring** runs the real row entry on modelled actors with a host double
whose reads are real requests to a real origin, so each script arms, waits
for and releases a real gate, and in daemon mode asks a modelled owner to
stand down.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import httpx2
import psutil
import pytest

from differential import harness, retirement_race
from differential.call_loss import EXPECTED_SECTIONS, INVALID, PERSON_TOOL
from differential.harness import RETURNED, measure_host_quit_row
from differential.retirement_race import (
    AFTER,
    DELIVERED,
    FAILED,
    IDLE_RACE_TIMEOUT_SECONDS,
    K1_NOT_APPLICABLE,
    K2_NOT_APPLICABLE,
    NO_SILENT_CUT,
    OWNER_GRACE_SECONDS,
    QUEUED,
    ROW_ADMISSION,
    ROW_CUT,
    ROW_DRAIN,
    ROW_QUEUED,
    ROW_REFUSED,
    ROW_RETIREMENT,
    SECOND_USERNAMES,
    SILENT,
    TURNOVER_CASES,
    TURNOVER_DRAIN_SECONDS,
    TURNOVER_IDLE_TIMEOUT_SECONDS,
    UNANSWERED,
    USERNAMES,
    W6_NOT_DISCHARGED,
    attempts_in,
    branch,
    call_classification,
    comparison_refusals,
    idle_margins,
    idle_problems,
    invalid_evidence,
    owner_log_reading,
    semantic_differences,
    semantics,
    turnover_problems,
)
from differential.synthetic_origin import (
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    RELEASED_BY_TEARDOWN,
    SERVED,
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

MS = 1_000_000


def _messages(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records]


# --- The lines the rows read, as the product writes them ----------------------------


def _attachment(tmp_path: Path, port: int = 51234) -> Any:
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
        package_version="4.20.1",
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


def _beating(monkeypatch, answer: Callable[[], Any]) -> None:
    from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

    async def beat(_attachment: Any, _call_id: str) -> Any:
        return answer()

    monkeypatch.setattr(FrontendCallHeartbeatMiddleware, "_beat", staticmethod(beat))


async def _preflight_lines(monkeypatch, caplog, tmp_path, answer) -> list[str]:
    from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

    _beating(monkeypatch, answer)
    attachment = _attachment(tmp_path)
    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_proxy"):
        await FrontendCallHeartbeatMiddleware(MagicMock())._preflight(
            attachment, "v1." + "0" * 32
        )
    return _messages(caplog)


async def test_a_retiring_owners_preflight_refusal_reads_as_retiring(
    monkeypatch, caplog, tmp_path
):
    from linkedin_mcp_server.daemon_descriptor import PROTOCOL_VERSION

    attachment = _attachment(tmp_path)
    instance = attachment.descriptor.instance_id

    def refused() -> Any:
        return httpx2.Response(
            409,
            json={
                "daemon": "retiring",
                "protocol": PROTOCOL_VERSION,
                "instance": instance,
            },
        )

    from linkedin_mcp_server.daemon_proxy import FrontendCallHeartbeatMiddleware

    _beating(monkeypatch, refused)
    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_proxy"):
        await FrontendCallHeartbeatMiddleware(MagicMock())._preflight(
            attachment, "v1." + "0" * 32
        )
    attempts = attempts_in(_messages(caplog))
    assert attempts == [{"attempt": "preflight", "classification": "retiring"}]
    assert call_classification(attempts) == "retiring"


async def test_an_unanswered_preflight_reads_as_unanswered(
    monkeypatch, caplog, tmp_path
):
    def gone() -> Any:
        raise httpx2.ConnectError("nothing is listening")

    lines = await _preflight_lines(monkeypatch, caplog, tmp_path, gone)
    attempts = attempts_in(lines)
    assert attempts == [{"attempt": "preflight", "classification": None}]
    assert call_classification(attempts) == UNANSWERED


async def test_a_go_ahead_is_no_attempt_line(monkeypatch, caplog, tmp_path):
    lines = await _preflight_lines(
        monkeypatch,
        caplog,
        tmp_path,
        lambda: httpx2.Response(200, json={"watched": False}),
    )
    assert attempts_in(lines) == []


async def test_the_owners_signed_refusal_reads_as_a_dispatch_refusal(
    monkeypatch, caplog, tmp_path
):
    from linkedin_mcp_server.daemon_liveness import RETIRING, _refusal, get_liveness
    from linkedin_mcp_server.daemon_proxy import (
        FrontendCallHeartbeatMiddleware,
        OwnerUnreachableError,
    )

    attachment = _attachment(tmp_path)
    get_liveness().serving_as(attachment.descriptor.instance_id)
    _beating(monkeypatch, lambda: httpx2.Response(200, json={"watched": False}))
    backend = SimpleNamespace(
        attachment_for_a_call=lambda: attachment,
        refuse_if_written_off=lambda _attachment: None,
    )

    async def refused_at_the_owner(_context: Any) -> Any:
        return _refusal(RETIRING, "shut down before this call started")

    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_proxy"):
        with pytest.raises(OwnerUnreachableError):
            await FrontendCallHeartbeatMiddleware(cast(Any, backend)).on_call_tool(
                MagicMock(), cast(Any, refused_at_the_owner)
            )
    attempts = attempts_in(_messages(caplog))
    assert attempts == [{"attempt": "dispatch", "classification": "retiring"}]


async def test_an_election_and_a_replay_read_from_the_frontends_lines(
    monkeypatch, caplog, tmp_path
):
    from linkedin_mcp_server.config.schema import AppConfig
    from linkedin_mcp_server.daemon import OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon_election import ElectionOutcome
    from linkedin_mcp_server.daemon_proxy import (
        DaemonProxyBackend,
        FrontendOwnerRecoveryMiddleware,
        OwnerUnreachableError,
    )

    first = _attachment(tmp_path)
    replacement = _attachment(tmp_path, port=51235)
    backend = DaemonProxyBackend(
        attachment=first,
        auth_root=tmp_path,
        profile=tmp_path / "profile",
        config=AppConfig(),
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.daemon_election.obtain_owner",
        lambda *_a, **_k: ElectionOutcome(
            OwnerLookup(state=OwnerState.ATTACHABLE, attachment=replacement),
            started_owner=True,
        ),
    )
    attempts_made: list[str] = []

    async def call_next(_context: Any) -> str:
        attempts_made.append("call")
        if len(attempts_made) == 1:
            raise OwnerUnreachableError(
                instance_id=first.descriptor.instance_id,
                nothing_was_sent=True,
                cause=httpx2.ConnectError("gone"),
            )
        return "read"

    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_proxy"):
        answer = await FrontendOwnerRecoveryMiddleware(backend).on_call_tool(
            MagicMock(), cast(Any, call_next)
        )
    assert answer == "read"
    assert attempts_in(_messages(caplog)) == [
        {"attempt": "election", "classification": "attached"},
        {"attempt": "replay", "classification": None},
    ]


def _stopped_server() -> tuple[Any, Any]:
    server = MagicMock()
    server.should_exit = False

    async def serves() -> None:
        while not server.should_exit:
            await asyncio.sleep(0.01)

    return server, asyncio.create_task(serves())


async def test_the_owners_idle_exit_line_is_the_one_the_rows_wait_for(caplog):
    from linkedin_mcp_server import daemon_liveness
    from linkedin_mcp_server.daemon_owner import _serve_until_stopped

    liveness = daemon_liveness.get_liveness()
    liveness.the_endpoint_is_live()
    liveness._quiet_since = liveness._quiet_since - 60  # ty: ignore
    server, serving = _stopped_server()
    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_owner"):
        await _serve_until_stopped(server, serving, [], 0.05, lock=None)
    assert any(retirement_race.OWNER_IDLE_LINE in line for line in _messages(caplog))


async def test_a_turnover_that_cuts_a_call_is_read_from_the_owners_lines(
    monkeypatch, caplog
):
    from linkedin_mcp_server import daemon_liveness, daemon_owner

    monkeypatch.setattr(daemon_owner, "_TURNOVER_DRAIN_SECONDS", 0.2)
    liveness = daemon_liveness.get_liveness()

    async def held() -> None:
        await asyncio.sleep(3600)

    task = liveness.admit(daemon_liveness.new_call_id(), held)
    assert task is not None
    server, serving = _stopped_server()
    with caplog.at_level(logging.INFO):
        await daemon_owner._serve_until_stopped(server, serving, ["asked"], lock=None)
    await asyncio.gather(task, return_exceptions=True)
    assert owner_log_reading(_messages(caplog)) == {
        "turned_over": True,
        "cut": 1,
        "cut_lines": 1,
    }


async def test_a_turnover_with_nothing_admitted_cuts_nothing(caplog):
    from linkedin_mcp_server import daemon_owner

    server, serving = _stopped_server()
    with caplog.at_level(logging.INFO):
        await daemon_owner._serve_until_stopped(server, serving, ["asked"], lock=None)
    assert owner_log_reading(_messages(caplog)) == {
        "turned_over": True,
        "cut": 0,
        "cut_lines": 0,
    }


async def test_directs_idle_close_line_is_the_one_the_rows_wait_for(
    monkeypatch, caplog
):
    from linkedin_mcp_server.drivers import browser

    lease = SimpleNamespace(handoff_requested=lambda: False, held_seconds=100.0)
    monkeypatch.setattr(browser, "_browser", object())
    monkeypatch.setattr(browser, "_browser_lease", lease)
    monkeypatch.setattr(browser, "_last_activity", time.monotonic() - 30)
    monkeypatch.setattr(browser, "_calls_in_flight", 0)
    monkeypatch.setattr(
        browser,
        "get_config",
        lambda: SimpleNamespace(
            browser=SimpleNamespace(
                browser_idle_timeout_seconds=IDLE_RACE_TIMEOUT_SECONDS,
                browser_min_hold_seconds=20.0,
            )
        ),
    )

    async def closed() -> bool:
        return True

    monkeypatch.setattr(browser, "_close_unless_a_call_arrived", closed)
    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.drivers.browser"):
        assert await browser.release_profile_if_idle_or_requested() is True
    assert any(retirement_race.DIRECT_IDLE_LINE in line for line in _messages(caplog))


def test_the_declared_values_are_the_products():
    from linkedin_mcp_server import daemon_owner
    from linkedin_mcp_server.config.schema import AppConfig

    server = daemon_owner.create_owner_server(
        config=AppConfig(), token="token", host="127.0.0.1", port=51234
    )
    assert server.config.timeout_graceful_shutdown == OWNER_GRACE_SECONDS
    assert daemon_owner._TURNOVER_DRAIN_SECONDS == TURNOVER_DRAIN_SECONDS
    assert retirement_race.WARM_TOOL == harness.READ_TOOL


def test_the_declared_times_hold_together():
    # Each hold stays inside the gate's deadline; the admission hold reaches
    # past the idle threshold and the grace; a cut read's second hold covers
    # the drain's end; the queued read is sent well before the stand-down.
    assert retirement_race.HOLD_CAP_SECONDS < GATE_DEADLINE_SECONDS
    assert (
        IDLE_RACE_TIMEOUT_SECONDS
        + OWNER_GRACE_SECONDS
        + retirement_race.PAST_THRESHOLD_SECONDS
        < retirement_race.HOLD_CAP_SECONDS
    )
    for case in TURNOVER_CASES.values():
        assert case.first_release < retirement_race.HOLD_CAP_SECONDS
        if not case.past_drain:
            assert case.first_release < TURNOVER_DRAIN_SECONDS / 2
    assert retirement_race.CUT_MARGIN_SECONDS > 0
    assert (
        TURNOVER_DRAIN_SECONDS + retirement_race.CUT_MARGIN_SECONDS
        < retirement_race.TURNOVER_EXIT_SECONDS
    )


# --- The stand-down the turnover rows send -----------------------------------------


def _identified(pid: int = 4242, instance: str = "owner-a") -> harness.OwnerIdentity:
    return harness.OwnerIdentity(pid, 1000.0, instance, "/root", object())


def _published(pid: int = 4242, instance: str = "owner-a") -> Any:
    return SimpleNamespace(
        pid=pid,
        instance_id=instance,
        url="http://127.0.0.1:51234/mcp",
        check_endpoint_is_local=lambda: None,
    )


_SECRET = "the-owners-secret-token"


def _recording(monkeypatch, published: Any, answer: httpx2.Response) -> list[Any]:
    from linkedin_mcp_server import daemon_owner

    sent: list[Any] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return answer

    monkeypatch.setattr(harness.daemon_descriptor, "read", lambda _root: published)
    monkeypatch.setattr(
        harness.daemon_descriptor, "read_token", lambda _root, _published: _SECRET
    )
    monkeypatch.setattr(
        daemon_owner,
        "direct_http_client",
        lambda *, timeout: httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    return sent


def test_the_stand_down_is_the_products_bodyless_request(monkeypatch, tmp_path):
    from linkedin_mcp_server.daemon_owner import STAND_DOWN_PATH

    sent = _recording(
        monkeypatch, _published(), httpx2.Response(200, json={"standing_down": True})
    )
    found = harness.ask_to_stand_down(
        harness.ActorAccount(tmp_path / "profile"), _identified()
    )

    [request] = sent
    assert request.method == "POST"
    assert request.url.path == STAND_DOWN_PATH
    assert request.content == b""
    assert request.headers["authorization"] == f"Bearer {_SECRET}"
    assert found["status"] == 200 and found["standing_down"] is True
    assert found["addressed"] is True and found["error"] is None
    assert found["sent_ns"] <= found["answered_ns"]
    assert _SECRET not in json.dumps(found)


def test_a_descriptor_naming_another_owner_gets_no_request(monkeypatch, tmp_path):
    sent = _recording(
        monkeypatch,
        _published(instance="owner-b"),
        httpx2.Response(200, json={"standing_down": True}),
    )
    found = harness.ask_to_stand_down(
        harness.ActorAccount(tmp_path / "profile"), _identified()
    )
    assert sent == []
    assert found["addressed"] is False and found["status"] is None
    assert "does not name the owner the row identified" in found["error"]


def test_a_refused_stand_down_is_recorded_without_its_token(monkeypatch, tmp_path):
    _recording(monkeypatch, _published(), httpx2.Response(401, json={"error": "x"}))
    found = harness.ask_to_stand_down(
        harness.ActorAccount(tmp_path / "profile"), _identified()
    )
    assert found["status"] == 401 and found["standing_down"] is None
    assert _SECRET not in json.dumps(found)


class _Process:
    def __init__(self, pid: int, ppid: int, start: float, on: str | None):
        self.pid = pid
        args = [f"--user-data-dir={on}"] if on else ["python"]
        self.info = {"cmdline": args, "exe": "/b/c", "status": psutil.STATUS_RUNNING}
        self._ppid, self._start = ppid, start

    def ppid(self) -> int:
        return self._ppid

    def create_time(self) -> float:
        return self._start


def test_the_roots_reading_names_one_lifetime_per_tree(tmp_path):
    account = harness.ActorAccount(tmp_path / "profile")
    tree = [
        _Process(500, 1, 1000.5, str(account.profile)),
        _Process(501, 500, 1000.6, str(account.profile)),
        _Process(600, 1, 999.0, None),
    ]
    point = harness.observe_roots("now", account, process_iter=lambda *_a: tree)
    assert point["roots"] == [[500, 1000.5]]
    assert point["label"] == "now" and isinstance(point["seen_ns"], int)
    # The reading spans its census: stamped as it began and as it was done.
    assert point["done_ns"] >= point["seen_ns"]
    second = [*tree, _Process(700, 1, 1001.0, str(account.profile))]
    assert (
        len(
            harness.observe_roots("now", account, process_iter=lambda *_a: second)[
                "roots"
            ]
        )
        == 2
    )


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


def _gate(path: str, entered: int, requested: int, *, terminal=SERVED) -> dict:
    return {
        "path": path,
        "ordinal": 1,
        "deadline_seconds": GATE_DEADLINE_SECONDS,
        "entered_monotonic_ns": entered * MS,
        "release_requested_monotonic_ns": requested * MS,
        "released_by": RELEASED_BY_ROW,
        "released_monotonic_ns": (requested + 5) * MS,
        "terminal": terminal,
        "wrote": terminal == SERVED,
    }


_A = [4242, 999.5, "owner-a"]


def _lifetime(
    pid: Any,
    start: Any,
    *,
    ppid: int = 1,
    digest: str = "owner",
    first: float | None = None,
    last: float | None = None,
) -> list:
    """One lifetime as ``harness.launch_lifetimes`` records it: pid, start,
    ppid, command digest, exit sample, first sample, last read."""
    return [
        pid,
        start,
        ppid,
        digest,
        None,
        start + 0.1 if first is None else first,
        start + 100.0 if last is None else last,
    ]


def _base(row: str, *, daemon: bool, idle: float) -> dict:
    record: dict[str, Any] = {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": idle,
        "k2": dict(K2_NOT_APPLICABLE),
        "observation_problems": [],
        "script_error": None,
        "username": USERNAMES[row],
        "host": _host(),
        "attempts_before": [],
        "cleanup_began_ns": 300_000 * MS,
    }
    if daemon:
        record.update(
            owner_identified=list(_A),
            owner_processes=[_lifetime(*_A[:2])],
            gate_processes=[],
            # The owner's own browser, as ``harness.browser_lineage`` records it.
            browser_roots=[[5000, 1000.5, None, *_A[:2]]],
            script_began=1000.95,
            published={"written": 999.0},
        )
    return record


def _admission(*, daemon: bool = True) -> dict:
    """Admission wins: the warm-up 0.1-0.9 s, the read sent at 1 s, its
    profile page held from 1.5 s to 15.9 s, past 0.9 + 8 + 5; the browser
    read at 1.6 s and 14 s; the owner or Direct's browser idle afterwards."""
    user = USERNAMES[ROW_ADMISSION]
    record = _base(ROW_ADMISSION, daemon=daemon, idle=IDLE_RACE_TIMEOUT_SECONDS)
    record.update(
        anchor_ns=950 * MS,
        calls=[
            _call(harness.READ_TOOL, 100, 900, is_error=False),
            _read(1_000, 17_500),
        ],
        gates=[_gate(person_path(user, "main_profile"), 1_500, 15_900)],
        requests=[_request("/feed/", 500), *_pages(user, 1_490, 16_000, 17_000)],
        roots=[
            {
                "label": "hold entered",
                "roots": [[5000, 1000.5]],
                "seen": 1001.6,
                "seen_ns": 1_600 * MS,
            },
            {
                "label": "past the threshold",
                "roots": [[5000, 1000.5]],
                "seen": 1014.0,
                "seen_ns": 14_000 * MS,
                "done_ns": 14_050 * MS,
            },
        ],
        read_window={"attempts": [], "idle_closes": 0},
    )
    if daemon:
        record.update(
            owner_after_read={
                "lifetime": _A[:2],
                "instance_id": _A[2],
                "alive": True,
                "seen_ns": 17_600 * MS,
            },
            after={"idle_lines": 1, "exit": {"how": "exited", "seen_ns": 26_000 * MS}},
        )
    else:
        record["after"] = {"idle_lines": 1, "browser_gone": {"remaining": []}}
    return record


_B = [4343, 1012.0, "owner-b"]


def _retirement(*, daemon: bool = True) -> dict:
    """Retirement wins: the idle line seen at 9.1 s, the owner gone at 11 s,
    the browser read with no root at 11.2 s; the call sent at 11.5 s through
    a successor started at 12 s, its pages at 16, 20 and 23 s."""
    user = USERNAMES[ROW_RETIREMENT]
    record = _base(ROW_RETIREMENT, daemon=daemon, idle=IDLE_RACE_TIMEOUT_SECONDS)
    record.update(
        anchor_ns=950 * MS,
        retirement={"line_seen_ns": 9_100 * MS},
        calls=[
            _call(harness.READ_TOOL, 100, 900, is_error=False),
            _read(11_500, 29_000),
        ],
        requests=[_request("/feed/", 500), *_pages(user, 16_000, 20_000, 23_000)],
        roots=[
            {"label": "retired", "roots": [], "seen": 1011.2, "seen_ns": 11_200 * MS},
            {
                "label": "after the call",
                "roots": [[6000, 1014.0]],
                "seen": 1030.0,
                "seen_ns": 30_000 * MS,
            },
        ],
    )
    if daemon:
        record["retirement"]["exit"] = {"how": "exited", "seen_ns": 11_000 * MS}
        record.update(
            read_window={
                "attempts": [
                    {"attempt": "preflight", "classification": None},
                    {"attempt": "election", "classification": "attached"},
                    {"attempt": "replay", "classification": None},
                ]
            },
            owner_after_read={
                "lifetime": _B[:2],
                "instance_id": _B[2],
                "alive": False,
                "seen_ns": 30_500 * MS,
            },
            owner_processes=[_lifetime(*_A[:2]), _lifetime(*_B[:2])],
            browser_roots=[
                [5000, 1000.5, 1010.9, *_A[:2]],
                [6000, 1014.0, None, *_B[:2]],
            ],
        )
    else:
        record["retirement"]["browser_gone"] = {"remaining": []}
        record["read_window"] = {"attempts": []}
    return record


def _turnover(row: str) -> dict:
    """A turnover lane. The read's profile page entered at 2 s; the stand-down
    sent at 4.05 s and answered at 4.1 s. Drain lanes: released at 12.1 s,
    the read done at 19 s, the owner gone at 24 s. Cut lanes: released at
    18.1 s, the experience page entered at 21 s, the read cut at 34.2 s, the
    owner gone at 38 s. A second read goes to a successor started at 41 s."""
    case = TURNOVER_CASES[row]
    user = USERNAMES[row]
    record = _base(row, daemon=True, idle=TURNOVER_IDLE_TIMEOUT_SECONDS)
    record.update(
        k1=dict(K1_NOT_APPLICABLE),
        w6=W6_NOT_DISCHARGED,
        stand_down={
            "addressed": True,
            "sent_ns": 4_050 * MS,
            "answered_ns": 4_100 * MS,
            "status": 200,
            "standing_down": True,
            "error": None,
        },
    )
    main = person_path(user, "main_profile")
    if case.past_drain:
        record.update(
            calls=[
                _call(harness.READ_TOOL, 100, 900, is_error=False),
                _read(
                    1_000,
                    34_200,
                    is_error=True,
                    marked_sections=[],
                    status="outcome_unknown",
                    retry_safe=False,
                ),
            ],
            gates=[
                _gate(main, 2_000, 18_100),
                _gate(
                    person_path(user, "experience"), 21_000, 37_100, terminal=PEER_GONE
                ),
            ],
            requests=[_request("/feed/", 500), *_pages(user, 1_990, 20_990)],
            owner_log={"turned_over": True, "cut": 1, "cut_lines": 1},
            owner_exit={"how": "exited", "seen_ns": 38_000 * MS, "seen": 1038.0},
        )
    else:
        record.update(
            calls=[
                _call(harness.READ_TOOL, 100, 900, is_error=False),
                _read(1_000, 19_000),
            ],
            gates=[_gate(main, 2_000, 12_100)],
            requests=[_request("/feed/", 500), *_pages(user, 1_990, 15_000, 18_000)],
            owner_log={"turned_over": True, "cut": 0, "cut_lines": 0},
            owner_exit={"how": "exited", "seen_ns": 24_000 * MS, "seen": 1024.0},
        )
    if case.second is not None:
        second = SECOND_USERNAMES[row]
        sent = 5_100 if case.second == AFTER else 2_010
        record["second_username"] = second
        record["calls"].append(_read(sent, 60_000))
        record["requests"] += _pages(second, 45_000, 48_000, 51_000)
        refusal = (
            {"attempt": "preflight", "classification": "retiring"}
            if case.second == AFTER
            else {"attempt": "dispatch", "classification": "retiring"}
        )
        record["second_attempts"] = [
            refusal,
            {"attempt": "election", "classification": "attached"},
            {"attempt": "replay", "classification": None},
        ]
        successor = [4545, 1041.0, "owner-c"]
        record["owner_after"] = {
            "lifetime": successor[:2],
            "instance_id": successor[2],
            "alive": False,
            "seen_ns": 61_000 * MS,
        }
        record["owner_processes"].append(_lifetime(*successor[:2]))
        record["browser_roots"].append([7000, 1043.0, None, *successor[:2]])
        if case.second == QUEUED:
            record["owner_log"]["cut"] = 2
            record["owner_log"]["cut_lines"] = 2
    return record


_VALID = [
    pytest.param(_admission, ROW_ADMISSION, True, id="admission-daemon"),
    pytest.param(
        lambda: _admission(daemon=False), ROW_ADMISSION, False, id="admission-direct"
    ),
    pytest.param(_retirement, ROW_RETIREMENT, True, id="retirement-daemon"),
    pytest.param(
        lambda: _retirement(daemon=False), ROW_RETIREMENT, False, id="retirement-direct"
    ),
    *[
        pytest.param(lambda row=row: _turnover(row), row, True, id=row)
        for row in TURNOVER_CASES
    ],
]


def _problems(record: dict, daemon: bool) -> list[str]:
    return retirement_race.problems_for(record, daemon=daemon)


@pytest.mark.parametrize(("build", "row", "daemon"), _VALID)
def test_a_valid_record_passes(build, row, daemon):
    record = build()
    assert record["row"] == row
    assert _problems(record, daemon) == []


def test_the_margins_the_idle_timeout_left_are_derived_from_the_packet():
    # The descriptor written at 999.0 s, the warm-up sent at 1000.1 s; the
    # warm-up ended at 0.9 s and the read sent at 1 s.
    assert idle_margins(_admission()) == {"startup": 6.9, "script": 7.9}
    assert idle_margins(_admission(daemon=False))["startup"] is None


def _gate_of(record: dict, index: int = 0) -> dict:
    return record["gates"][index]


def _call_of(record: dict, index: int) -> dict:
    return record["calls"][index]


def _add(record: dict, path: str, ms: int) -> None:
    record["requests"].append(_request(path, ms))


_ADMIT_USER = USERNAMES[ROW_ADMISSION]
_RETIRE_USER = USERNAMES[ROW_RETIREMENT]

#: (builder, daemon, change, the problem it must bring, whether invalid).
_CONTROLS = [
    # Admission wins: an idle cut of an admitted call fails, as a finding.
    pytest.param(
        _admission,
        True,
        lambda r: _gate_of(r).update(terminal=PEER_GONE),
        "an idle cut",
        False,
        id="admission-held-request-cut",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _call_of(r, 1).update(outcome="raised", is_error=None),
        "the held read did not complete normally",
        False,
        id="admission-read-failed",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["read_window"].update(
            attempts=[{"attempt": "preflight", "classification": "retiring"}]
        ),
        "the frontend met a refusal or an election during the held read",
        False,
        id="admission-refused-mid-read",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["owner_processes"].append(_lifetime(*_B[:2])),
        "the read did not complete on the original owner",
        False,
        id="admission-served-by-a-successor",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["owner_after_read"].update(alive=False),
        "the read did not complete on the original owner",
        False,
        id="admission-owner-gone-at-the-read",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["roots"][1].update(roots=[[5001, 1013.0]]),
        "the browser's lifetime changed across the hold",
        False,
        id="admission-browser-reopened",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: [point.update(roots=[[5000, 1009.0]]) for point in r["roots"]],
        "the browser's lifetime changed across the hold",
        False,
        id="admission-browser-not-the-warm-ups",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["after"].update(idle_lines=0),
        "the owner is not shown to idle out after the read",
        False,
        id="admission-owner-never-retired",
    ),
    pytest.param(
        _admission,
        False,
        lambda r: r["read_window"].update(idle_closes=1),
        "Direct closed its idle browser during the held read",
        False,
        id="admission-direct-closed-under-the-read",
    ),
    pytest.param(
        _admission,
        False,
        lambda r: r["after"].update(idle_lines=0),
        "Direct is not shown to close its idle browser after the read",
        False,
        id="admission-direct-never-closed",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _add(r, person_path(_ADMIT_USER, "experience"), 15_000),
        "the experience page was requested 2 times",
        False,
        id="admission-page-twice",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["requests"][2].update(monotonic_ns=15_000 * MS),
        "the experience page was requested before the release",
        False,
        id="admission-went-past-the-hold",
    ),
    # Admission wins: what leaves the race unmeasured.
    pytest.param(
        _admission,
        True,
        lambda r: r["owner_processes"].append(_lifetime(4343, 1000.5)),
        "the idle timeout is too small for this runner",
        True,
        id="admission-owner-retired-before-first-admission",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r.update(
            attempts_before=[{"attempt": "election", "classification": "attached"}]
        ),
        "the frontend met a refusal or an election before the race",
        True,
        id="admission-election-before-the-race",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _call_of(r, 1).update(began_monotonic_ns=9_000 * MS),
        "the read was not sent inside the 8.0s quiet period",
        True,
        id="admission-read-sent-too-late",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _gate_of(r).update(entered_monotonic_ns=9_000 * MS),
        "the held page entered after the idle threshold",
        True,
        id="admission-entered-too-late",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _gate_of(r).update(release_requested_monotonic_ns=12_000 * MS),
        "the hold did not last past the idle threshold",
        True,
        id="admission-released-too-early",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _gate_of(r).update(terminal=DEADLINE),
        "the hold ran out its deadline",
        True,
        id="admission-gate-deadline",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: _gate_of(r).update(released_by=RELEASED_BY_TEARDOWN),
        "the hold was released by 'teardown'",
        True,
        id="admission-teardown-release",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["roots"][1].update(seen_ns=10_000 * MS),
        "the browser was read before an idle cut could land",
        True,
        id="admission-browser-read-early",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["roots"][1].update(roots=None),
        "the browser could not be read",
        True,
        id="admission-browser-unread",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r.update(roots=[]),
        "the browser was not read across the hold",
        True,
        id="admission-no-roots",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r["calls"].pop(0),
        "the warm-up read is not recorded as returned",
        True,
        id="admission-no-warm-up",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r.update(k2=None),
        "the record does not say why K2 is not applicable",
        False,
        id="admission-k2-unexplained",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r.pop("owner_processes"),
        "the row's owner launches or its script's start were not recorded",
        True,
        id="admission-launches-unrecorded",
    ),
    pytest.param(
        _admission,
        True,
        lambda r: r.update(owner_processes=[]),
        "the read did not complete on the original owner",
        False,
        id="admission-owner-never-launched",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["owner_processes"].append(_lifetime(4646, 1020.0)),
        "the row launched other owners besides the successor",
        False,
        id="retirement-another-owner-launched",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["gate_processes"].extend(
            [
                _lifetime(7000, 1000.0, digest="gate"),
                _lifetime(7001, 1011.9, digest="g2"),
                _lifetime(7002, 1020.0, digest="g3"),
            ]
        ),
        "the row launched other owners besides the successor",
        False,
        id="retirement-gate-beyond-the-launches",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: r.update(username=USERNAMES[ROW_QUEUED]),
        "the record names 'synthetic-queued' as its username",
        True,
        id="cut-another-username",
    ),
    # Retirement wins.
    pytest.param(
        _retirement,
        True,
        lambda r: r.update(retirement={}),
        "no positive retirement evidence",
        True,
        id="retirement-no-evidence",
    ),
    pytest.param(
        _retirement,
        False,
        lambda r: r.update(retirement={}),
        "no positive retirement evidence",
        True,
        id="retirement-direct-no-evidence",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["retirement"].update(line_seen_ns=12_000 * MS),
        "the call was not sent after the retirement was seen",
        True,
        id="retirement-call-before-evidence",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["owner_processes"].append(_lifetime(4343, 1000.5)),
        "the idle timeout is too small for this runner",
        True,
        id="retirement-owner-retired-before-first-admission",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: _call_of(r, 1).update(is_error=False, marked_sections=[]),
        "the call after the retirement was cut silently",
        False,
        id="retirement-silent-cut",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: _call_of(r, 1).update(outcome="cancelled"),
        "the call after the retirement was cut silently",
        False,
        id="retirement-cancelled",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["owner_after_read"].update(lifetime=_A[:2], instance_id=_A[2]),
        "the call was answered by the retiring owner, not a successor",
        False,
        id="retirement-answered-by-the-retired-owner",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: (
            r["owner_after_read"].update(lifetime=[4343, 1005.0]),
            r["owner_processes"].__setitem__(1, _lifetime(4343, 1005.0)),
        ),
        "the successor was not started by this call",
        False,
        id="retirement-successor-borrowed-from-another-call",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r.update(
            requests=[
                _request("/feed/", 500),
                *_pages(_RETIRE_USER, 5_000, 6_000, 7_000),
            ]
        ),
        "page was not requested inside the read's interval",
        False,
        id="retirement-read-borrowed-from-another-call",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["owner_processes"].pop(),
        "the successor is not an owner the row was seen to launch",
        False,
        id="retirement-successor-unseen",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["owner_after_read"].update(lifetime=None, problem="no owner"),
        "no successor is published after the call",
        False,
        id="retirement-no-successor",
    ),
    pytest.param(
        _retirement,
        True,
        lambda r: r["read_window"].update(
            attempts=[{"attempt": "preflight", "classification": "token_rejected"}]
        ),
        "classified the retiring owner as 'token_rejected'",
        False,
        id="retirement-wrong-classification",
    ),
    pytest.param(
        _retirement,
        False,
        lambda r: r["roots"][1].update(roots=[[5000, 1000.5]]),
        "not shown reopened after the close",
        False,
        id="retirement-direct-never-closed",
    ),
    pytest.param(
        _retirement,
        False,
        lambda r: r["retirement"].update(browser_gone={"remaining": [5000]}),
        "Direct's browser is not shown closed after its idle line",
        False,
        id="retirement-direct-browser-stayed",
    ),
    # Turnover: the drain.
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: _call_of(r, 1).update(
            is_error=True,
            marked_sections=[],
            status="outcome_unknown",
            retry_safe=False,
        ),
        "the admitted work did not complete normally inside the drain",
        False,
        id="drain-cut-anyway",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["owner_log"].update(cut=1),
        "the owner cut a call though its admitted work finished in time",
        False,
        id="drain-cut-line",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["owner_exit"].update(how="still running"),
        "the owner is not shown to stand down",
        False,
        id="drain-owner-stayed",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["owner_log"].update(turned_over=False),
        "the owner's log does not show it standing down",
        False,
        id="drain-no-turnover-line",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["owner_processes"].append(_lifetime(*_B[:2])),
        "an owner was launched though nothing called after the turnover",
        False,
        id="drain-owner-started",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: _call_of(r, 1).update(ended_monotonic_ns=35_000 * MS),
        "the held work did not finish inside the drain",
        True,
        id="drain-outlasted-the-drain",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["stand_down"].update(status=401, standing_down=None),
        "the stand-down was not answered as standing down",
        True,
        id="drain-stand-down-refused",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: r["stand_down"].update(sent_ns=1_000 * MS),
        "the stand-down was sent before the read's body began",
        True,
        id="drain-stand-down-too-early",
    ),
    pytest.param(
        lambda: _turnover(ROW_DRAIN),
        True,
        lambda r: _gate_of(r).update(terminal=DEADLINE),
        "the first hold did not end as the row released it",
        True,
        id="drain-first-hold-deadline",
    ),
    # Turnover: new work refused.
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: _add(
            r, person_path(SECOND_USERNAMES[ROW_REFUSED], "main_profile"), 20_000
        ),
        "the new work ran on the retiring owner",
        False,
        id="refused-ran-on-the-retiring-owner",
    ),
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: r.update(
            requests=[
                req
                for req in r["requests"]
                if SECOND_USERNAMES[ROW_REFUSED] not in req["path"]
            ]
            + _pages(SECOND_USERNAMES[ROW_REFUSED], 20_000, 21_000, 22_000)
        ),
        "the new work ran on the retiring owner: 3 of its pages",
        False,
        id="refused-read-before-the-owner-left",
    ),
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: r.update(second_attempts=[]),
        "the frontend reported no refusal of the call",
        False,
        id="refused-never-refused",
    ),
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: _call_of(r, 2).update(is_error=False, marked_sections=[]),
        "the second read was cut silently",
        False,
        id="refused-silent",
    ),
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: (
            r["owner_after"].update(lifetime=[4545, 1001.0]),
            r["owner_processes"].__setitem__(1, _lifetime(4545, 1001.0)),
        ),
        "the successor was not started by this call",
        False,
        id="refused-successor-borrowed",
    ),
    pytest.param(
        lambda: _turnover(ROW_REFUSED),
        True,
        lambda r: _call_of(r, 2).update(began_monotonic_ns=25_000 * MS),
        "the new work was not sent after the stand-down and while the held work",
        True,
        id="refused-sent-after-the-drain",
    ),
    # Turnover: a read cut past the drain.
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _call_of(r, 1).update(
            is_error=False,
            marked_sections=list(EXPECTED_SECTIONS),
            status=None,
            retry_safe=None,
        ),
        "the cut read was not reported as an unknown outcome",
        False,
        id="cut-reported-as-success",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _call_of(r, 1).update(retry_safe=True),
        "the cut read was not reported as an unknown outcome unsafe to retry",
        False,
        id="cut-said-retry-safe",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _add(r, person_path(USERNAMES[ROW_CUT], "main_profile"), 40_000),
        "the cut read ran again",
        False,
        id="cut-replayed",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _add(r, person_path(USERNAMES[ROW_CUT], "education"), 33_000),
        "the cut read went on after the cut: its education page was requested",
        False,
        id="cut-went-on",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: r["owner_log"].update(cut=0),
        "nothing was cut",
        True,
        id="cut-nothing-cut",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _call_of(r, 1).update(ended_monotonic_ns=30_000 * MS),
        "the held read ended inside the drain",
        True,
        id="cut-ended-inside",
    ),
    pytest.param(
        lambda: _turnover(ROW_CUT),
        True,
        lambda r: _gate_of(r, 1).update(terminal=DEADLINE),
        "the second hold ran out its deadline",
        True,
        id="cut-second-hold-deadline",
    ),
    # Turnover: a queued read cut before its body began.
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: _call_of(r, 2).update(
            is_error=True,
            marked_sections=[],
            status="outcome_unknown",
            retry_safe=False,
        ),
        "the queued read was reported as an unknown outcome",
        False,
        id="queued-confused-with-cut",
    ),
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: r.update(
            second_attempts=[{"attempt": "preflight", "classification": "retiring"}]
        ),
        "the queued read is not shown refused as not run",
        False,
        id="queued-not-refused-as-not-run",
    ),
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: _add(
            r, person_path(SECOND_USERNAMES[ROW_QUEUED], "main_profile"), 30_000
        ),
        "the queued read ran on the retiring owner",
        False,
        id="queued-ran-on-the-retiring-owner",
    ),
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: r["owner_log"].update(cut=1),
        "the queued read is not shown admitted when the drain ran out",
        True,
        id="queued-never-admitted",
    ),
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: _call_of(r, 2).update(began_monotonic_ns=4_500 * MS),
        "the queued read was not sent before the stand-down",
        True,
        id="queued-sent-after-the-stand-down",
    ),
    pytest.param(
        lambda: _turnover(ROW_QUEUED),
        True,
        lambda r: _call_of(r, 1).update(
            outcome="raised", is_error=None, status=None, retry_safe=None
        ),
        "the cut read was not reported as an unknown outcome",
        False,
        id="queued-begun-read-not-unknown",
    ),
]


@pytest.mark.parametrize(
    ("build", "daemon", "change", "expected", "invalid"), _CONTROLS
)
def test_one_changed_observation_fails_the_record(
    build, daemon, change, expected, invalid
):
    record = build() if daemon else build(daemon=False)
    change(record)
    problems = _problems(record, daemon)
    matching = [problem for problem in problems if expected in problem]
    assert matching, problems
    # Invalid evidence is named as such, and a finding never is.
    assert all(p.startswith(INVALID) is invalid for p in matching), matching


def test_windows_counts_a_venv_launcher_and_its_gate_as_one_owner_launch():
    """The shape measured on windows-latest: a venv launcher of the release
    gate, the interpreter it started, and the owner the gate started through
    its own launcher. One launch, so the read stayed on the original owner;
    a second gate before the race is a replacement attempted."""
    record = _admission()
    record["platform"] = "win32"
    launcher = _lifetime(7256, 999.3, ppid=3628, digest="gate", last=1090.0)
    gate = _lifetime(7884, 999.31, ppid=7256, digest="gate", first=999.4)
    record["gate_processes"] = [launcher, gate]
    record["owner_processes"] = [
        _lifetime(2524, 999.49, ppid=7884, digest="owner", last=1090.0),
        _lifetime(_A[0], _A[1], ppid=2524, digest="owner", first=999.6),
    ]
    assert _problems(record, True) == []

    record["gate_processes"].append(_lifetime(9000, 1000.5, digest="gate-2"))
    assert any("the idle timeout is too small" in p for p in _problems(record, True))
    # One gate past the race is an owner start attempted during it.
    record["gate_processes"] = [launcher, gate, _lifetime(9001, 1002.0, digest="g")]
    assert any(
        "the read did not complete on the original owner" in p
        for p in _problems(record, True)
    )


def test_a_retirement_followed_by_an_explicit_failure_passes_as_its_own_branch():
    for daemon in (True, False):
        record = _retirement(daemon=daemon)
        record["calls"][1].update(outcome="raised", exception="McpError", is_error=None)
        record["requests"] = record["requests"][:1]
        assert branch(record["calls"][1]) == FAILED
        assert _problems(record, daemon) == [], daemon
        assert branch(_retirement(daemon=daemon)["calls"][1]) == DELIVERED
        # K0 sees both safe branches as one; a silent cut is not one of them.
        assert semantics(record)["call"] == NO_SILENT_CUT
        assert semantics(_retirement(daemon=daemon))["call"] == NO_SILENT_CUT


def test_the_new_work_may_fail_explicitly_but_never_silently():
    record = _turnover(ROW_REFUSED)
    record["calls"][2].update(is_error=True, marked_sections=[])
    record["requests"] = [
        r for r in record["requests"] if SECOND_USERNAMES[ROW_REFUSED] not in r["path"]
    ]
    record["owner_after"] = {"lifetime": None, "problem": "no owner is published"}
    assert _problems(record, True) == []
    record["calls"][2].update(is_error=False)
    assert branch(record["calls"][2]) == SILENT


def test_a_result_naming_every_section_it_missed_is_an_explicit_failure():
    """The shape frozen Direct returned on windows-latest after its browser
    idled closed: the profile read, both sections named in
    ``section_errors``. The caller is told; that is no silent cut. A section
    missing without being named is."""
    record = _retirement(daemon=False)
    record["calls"][-1].update(
        sections=["main_profile"],
        marked_sections=["main_profile"],
        section_errors=["education", "experience"],
    )
    assert branch(record["calls"][-1]) == FAILED
    assert _problems(record, False) == []
    record["calls"][-1].update(section_errors=["education"])
    assert branch(record["calls"][-1]) == SILENT


def test_election_candidates_that_exited_are_no_second_owner():
    """While a retiring owner holds the lock the election starts candidates
    on its backoff, and each exits: measured on three legs during the
    turnover drain. One the watcher did not see exit still fails."""
    record = _turnover(ROW_REFUSED)
    for pid, start in ((4701, 1012.0), (4702, 1013.0), (4703, 1015.0)):
        candidate = _lifetime(pid, start)
        candidate[4] = start + 0.8
        record["owner_processes"].append(candidate)
    assert _problems(record, True) == []
    record["owner_processes"].append(_lifetime(4704, 1016.0))
    problems = _problems(record, True)
    assert any("other owners besides the successor" in p for p in problems), problems


def test_a_hold_served_past_its_deadline_is_invalid():
    """Released on time, but served 21 s after it entered: still labelled
    served, and the row declared no such hold."""
    record = _admission()
    gate = record["gates"][0]
    gate["released_monotonic_ns"] = gate["entered_monotonic_ns"] + 21_000 * MS
    problems = _problems(record, True)
    assert any("past the gate's" in p for p in problems), problems
    assert all(p.startswith(INVALID) for p in problems), problems
    del gate["released_monotonic_ns"]
    assert any("has no end time" in p for p in _problems(record, True))


def test_a_reading_begun_in_the_hold_but_done_after_it_is_invalid():
    """The census started inside the hold and read the roots after the
    release: its stamp alone would place it inside."""
    record = _admission()
    point = next(p for p in record["roots"] if p["label"] == "past the threshold")
    point["done_ns"] = 15_950 * MS
    problems = _problems(record, True)
    assert any("read after the hold ended" in p for p in problems), problems
    assert all(p.startswith(INVALID) for p in problems), problems
    del point["done_ns"]
    assert any("has no end" in p for p in _problems(record, True))


def test_a_candidate_that_ran_a_browser_may_have_read_and_is_no_loser():
    """An owner releases its lock before it exits, so a candidate can take it
    while the retiring process is still alive, read, and exit before it. Its
    process timing says nothing; the browser it launched does. A browser
    nobody can attribute leaves every candidate unexcused too."""
    record = _turnover(ROW_REFUSED)
    candidate = _lifetime(4705, 1015.0)
    candidate[4] = 1030.0
    record["owner_processes"].append(candidate)
    assert _problems(record, True) == []
    record["browser_roots"].append([7100, 1016.0, 1029.0, 4705, 1015.0])
    problems = _problems(record, True)
    assert any("other owners besides the successor" in p for p in problems), problems
    record["browser_roots"][-1][3:] = [None, None]
    problems = _problems(record, True)
    assert any("other owners besides the successor" in p for p in problems), problems


def test_a_windows_candidate_needs_every_process_of_its_launch_gone():
    """A venv launcher seen gone while the interpreter it started was never
    seen gone: the launch is not shown to have lost."""
    record = _turnover(ROW_REFUSED)
    record["platform"] = "win32"
    launcher = _lifetime(4801, 1012.0, digest="candidate", last=1013.5)
    launcher[4] = 1013.6
    interpreter = _lifetime(4802, 1012.1, ppid=4801, digest="candidate", first=1012.2)
    record["owner_processes"] += [launcher, interpreter]
    problems = _problems(record, True)
    assert any("other owners besides the successor" in p for p in problems), problems
    interpreter[4] = 1013.4
    assert _problems(record, True) == []


def test_a_page_no_browser_of_the_successor_was_alive_for_was_not_its_read():
    """The successor's browser started only after the call's first page was
    asked for: some other browser read it, whatever the processes' timing."""
    record = _turnover(ROW_REFUSED)
    second = SECOND_USERNAMES[ROW_REFUSED]
    first_page = min(
        r["t"] for r in record["requests"] if r["path"].startswith(f"/in/{second}/")
    )
    record["browser_roots"][-1][1] = first_page + 3.0
    problems = _problems(record, True)
    assert any("the successor did not read them" in p for p in problems), problems
    # Nor does a browser that was gone before the page.
    record["browser_roots"][-1][1:3] = [1042.0, first_page - 1.0]
    problems = _problems(record, True)
    assert any("the successor did not read them" in p for p in problems), problems
    del record["browser_roots"]
    problems = _problems(record, True)
    assert any("were not recorded" in p for p in problems), problems


def test_a_refusal_by_an_owner_already_gone_reads_as_unanswered():
    record = _turnover(ROW_REFUSED)
    record["second_attempts"][0] = {"attempt": "preflight", "classification": None}
    assert _problems(record, True) == []


def test_invalid_evidence_is_told_apart_from_a_finding():
    record = _admission()
    record["attempts_before"] = [{"attempt": "election", "classification": "attached"}]
    record["gates"][0]["terminal"] = PEER_GONE
    problems = _problems(record, True)
    assert [p for p in problems if p not in invalid_evidence(problems)] == [
        "the held request's browser went away while it was held: an idle cut"
    ]
    assert len(invalid_evidence(problems)) == 1


def test_a_turnover_lane_has_no_direct_column():
    assert turnover_problems(_turnover(ROW_DRAIN), daemon=False) == [
        "a turnover lane runs only through an owner; K1 is not applicable"
    ]
    record = _turnover(ROW_DRAIN)
    record.update(k1=None, w6=None)
    problems = _problems(record, True)
    assert "the record does not say why K1 is not applicable" in problems
    assert "the record does not say that W6 stays open" in problems


def test_a_record_that_is_missing_or_for_another_row_fails():
    assert idle_problems(None, daemon=True) == ["the row kept no record"]
    assert turnover_problems(None, daemon=True) == ["the row kept no record"]
    assert idle_problems({"row": "H-CAL"}, daemon=True) == [
        "the record is for row 'H-CAL', which races no idle retirement"
    ]
    assert turnover_problems({"row": "H-CAL"}, daemon=True) == [
        "the record is for row 'H-CAL', which turns no owner over"
    ]
    record = _admission()
    record["mode"] = "direct"
    assert "the record is for mode 'direct', not daemon" in _problems(record, True)
    record = _admission()
    record["idle_timeout_seconds"] = 20.0
    assert any("an idle timeout of 20.0" in p for p in _problems(record, True))


def test_the_repeat_compares_classifications_and_refuses_an_invalid_record():
    reference, repeat = _retirement(), _retirement()
    # The other safe branch, and another classification: a race either way.
    repeat["calls"][1].update(outcome="raised", is_error=None)
    repeat["requests"] = repeat["requests"][:1]
    repeat["read_window"]["attempts"][0]["classification"] = "retiring"
    assert semantic_differences(reference, repeat) == []
    for row in TURNOVER_CASES:
        assert semantic_differences(_turnover(row), _turnover(row)) == [], row
    broken = _admission()
    broken["gates"][0]["terminal"] = PEER_GONE
    [refusal] = semantic_differences(_admission(), broken)
    assert refusal.startswith("the repeat record is not valid")
    assert semantic_differences(None, _admission())[0].startswith(
        "the reference record is not valid"
    )


def test_the_candidate_is_held_to_direct_only_from_two_valid_records():
    for build in (_admission, _retirement):
        assert comparison_refusals(build(daemon=False), build()) == []
    cut = _admission()
    cut["gates"][0]["terminal"] = PEER_GONE
    [refusal] = comparison_refusals(_admission(daemon=False), cut)
    assert refusal.startswith("the daemon record is not valid")
    [refusal] = comparison_refusals(None, _admission())
    assert refusal.startswith("the Direct record is not valid")


def test_every_row_here_is_declared_with_its_verdict_and_seams():
    for row in retirement_race.IDLE_ROWS:
        lifecycle = harness.ROWS[row]
        assert lifecycle.idle_timeout == IDLE_RACE_TIMEOUT_SECONDS
        assert lifecycle.race and not lifecycle.stands_down
        assert harness.ROW_VERDICTS[row] is retirement_race.idle_problems
    assert harness.ROWS[ROW_RETIREMENT].successor
    assert not harness.ROWS[ROW_ADMISSION].successor
    for row, case in TURNOVER_CASES.items():
        lifecycle = harness.ROWS[row]
        assert lifecycle.idle_timeout == TURNOVER_IDLE_TIMEOUT_SECONDS
        assert lifecycle.race and lifecycle.stands_down
        assert lifecycle.successor is case.successor
        assert harness.ROW_VERDICTS[row] is retirement_race.turnover_problems


def test_a_stand_down_or_a_race_is_declared_only_with_its_script_and_alone():
    lifecycle = harness.RowLifecycle(stands_down=True)
    assert harness.lifecycle_problems("H-NEW", lifecycle) == [
        "a race with no script to run it",
        "a race that combines with other scenarios",
        "a stand-down with no race seams to send it",
    ]


# --- The rows through the row entry -------------------------------------------------


class _RaceScene(_CalibrationScene):
    """The real row entry on modelled actors, for a race against retirement.

    The host double reads the person pages as real requests, the held one at
    the real gate. Direct is modelled as its idle close works: once
    ``idle`` seconds pass with no call in flight, its line goes to the host's
    stderr and the browser closes; a call reopens it. ``closes_under_call``
    closes it under the held call instead, as an idle close that ignored the
    call would.
    """

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.idle = 0.4
        self.browser: list[Any] | None = None
        self.next_pid = 5000
        self.in_flight = 0
        self.closes_under_call = False
        self.never_closes = False
        #: Direct closes its idle browser once here, so a reading right after
        #: a call never races a second close.
        self.closed = False
        self.on_stderr: Callable[[str], None] = lambda _line: None
        self.background: list[asyncio.Future[Any]] = []
        self.last_end = time.monotonic()
        for name, value in (
            ("IDLE_RACE_TIMEOUT_SECONDS", self.idle),
            ("OWNER_GRACE_SECONDS", 0.2),
            ("PAST_THRESHOLD_SECONDS", 0.3),
            ("RETIREMENT_EVIDENCE_SECONDS", 3.0),
            ("BROWSER_GONE_SECONDS", 1.0),
        ):
            monkeypatch.setattr(retirement_race, name, value)
        monkeypatch.setattr(harness, "observe_roots", self.roots)

    def roots(self, label, _account, **_kw) -> dict:
        return {
            "label": label,
            "roots": [list(self.browser)] if self.browser else [],
            "seen": time.time(),
            "seen_ns": time.monotonic_ns(),
            "done_ns": time.monotonic_ns(),
        }

    def _close(self) -> None:
        self.browser = None
        self.on_stderr(
            f"INFO Closing idle browser after {self.idle:.0f}s and releasing the profile"
        )

    async def _idle_closer(self) -> None:
        while True:
            await asyncio.sleep(0.05)
            quiet = time.monotonic() - self.last_end
            if (
                self.browser
                and not self.never_closes
                and not self.closed
                and (self.in_flight == 0 or self.closes_under_call)
                and quiet >= self.idle
            ):
                self.closed = True
                self._close()

    async def _call(self, session, name: str, arguments: dict) -> dict:
        self.in_flight += 1
        if self.browser is None:
            self.browser = [self.next_pid, time.time()]
            self.next_pid += 1
        try:
            return await super()._call(session, name, arguments)
        finally:
            self.in_flight -= 1
            self.last_end = time.monotonic()

    async def host(self, *args, **kw):
        self.on_stderr = kw.get("on_stderr") or self.on_stderr
        closer = asyncio.ensure_future(self._idle_closer())
        try:
            return await super().host(*args, **kw)
        finally:
            closer.cancel()
            await asyncio.gather(closer, return_exceptions=True)

    async def run(self, row: str = ROW_ADMISSION, **options):
        return await measure_host_quit_row(
            profile=self.tmp_path / "auth" / "profile",
            experiment="K1",
            daemon=False,
            egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
            log=self.log,
            work_dir=self.tmp_path / "row",
            row=row,
            idle_timeout=self.idle,
            **options,
        )


@pytest.fixture
def racing(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _RaceScene(monkeypatch, tmp_path, profile, origin, certificates)


def _phases(scene) -> list[str]:
    return [e["name"] for e in scene.log.records() if e["kind"] == "phase"]


async def test_an_admitted_read_held_past_the_threshold_passes(racing):
    result = await racing.run(ROW_ADMISSION)

    assert result.failures == [], result.failures
    record = racing.published()["record"]
    assert record["problems"] == []
    [gate] = record["gates"]
    assert gate["path"] == person_path(_ADMIT_USER, "main_profile")
    assert gate["terminal"] == SERVED and gate["released_by"] == RELEASED_BY_ROW
    # One browser across the hold, closed only after the read.
    assert [len(point["roots"]) for point in record["roots"]] == [1, 1]
    assert record["after"]["idle_lines"] == 1
    assert _phases(racing) == [
        "armed",
        "entered",
        "past the threshold",
        "released",
        "returned",
        "retired",
    ]


async def test_an_idle_close_under_the_admitted_read_fails_the_row(racing):
    racing.closes_under_call = True
    result = await racing.run(ROW_ADMISSION)

    findings = [p for p in result.record["problems"] if not p.startswith(INVALID)]
    assert any(
        "Direct closed its idle browser during the held read" in p for p in findings
    )


async def test_a_call_after_the_browser_idled_out_reopens_it_and_passes(racing):
    result = await racing.run(ROW_RETIREMENT)

    assert result.failures == [], result.failures
    record = racing.published()["record"]
    assert record["problems"] == []
    retired, after = record["roots"]
    assert retired["roots"] == [] and len(after["roots"]) == 1
    assert _phases(racing) == ["retired", "called", "returned"]


async def test_a_retirement_never_seen_makes_no_call_and_is_invalid(racing):
    racing.never_closes = True
    result = await racing.run(ROW_RETIREMENT)

    problems = result.record["problems"]
    assert problems and invalid_evidence(problems) == problems, problems
    assert [c["tool"] for c in result.record["calls"]] == [harness.READ_TOOL]


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
        "cmdline": ["python", "-m", "linkedin_mcp_server.daemon_owner"],
    }


def test_each_browser_root_is_tied_to_the_process_that_drove_it():
    """Root, its children, the driver and the owner as the watcher records
    them: one root, named with the owner behind its driver and when it went.
    A child is inside its root's tree; a pid reused by a later process is not
    an earlier child's parent; a root whose driver was never seen names no
    launcher."""

    def start(actor: str, pid: int, ppid: int, at: float) -> dict:
        return {
            "kind": "process.start",
            "actor": actor,
            "in_row": True,
            "pid": pid,
            "ppid": ppid,
            "start_identity": at,
        }

    events = [
        # An earlier process with the driver's pid, long gone: not the parent.
        start("other", 11, 1, 50.0),
        start("owner", 10, 1, 100.0),
        start("driver", 11, 10, 101.0),
        start("browser", 12, 11, 102.0),
        start("browser", 13, 12, 102.5),
        {"kind": "process.exit", "pid": 12, "start_identity": 102.0, "t": 150.0},
        # The driver's pid, reused only after this root started.
        start("other", 11, 1, 300.0),
        start("browser", 20, 19, 200.0),
    ]
    assert harness.browser_lineage(events) == [
        [12, 102.0, 150.0, 10, 100.0],
        [20, 200.0, None, None, None],
    ]


def test_a_browser_first_seen_before_its_exec_still_counts_as_the_launchs_read():
    """A child sampled between fork and exec carries its driver's command
    line, and only a later record says it became the browser. It is still the
    candidate's browser, so the candidate is not excused; and a process born
    after the browser is never its parent."""

    def seen(kind: str, actor: str, pid: int, ppid: int, at: float, **more) -> dict:
        return {
            "kind": kind,
            "actor": actor,
            "in_row": True,
            "pid": pid,
            "ppid": ppid,
            "start_identity": at,
            **more,
        }

    events = [
        seen("process.start", "owner", 4705, 1, 1015.0),
        seen("process.start", "driver", 4706, 4705, 1015.2),
        seen("process.start", "driver", 4707, 4706, 1015.4),
        seen("process.update", "browser", 4707, 4706, 1015.4),
        seen("process.exit", "browser", 4707, 4706, 1015.4, t=1020.0),
    ]
    roots = harness.browser_lineage(events)
    assert roots == [[4707, 1015.4, 1020.0, 4705, 1015.0]]
    record = _turnover(ROW_REFUSED)
    candidate = _lifetime(4705, 1015.0)
    candidate[4] = 1021.0
    record["owner_processes"].append(candidate)
    record["browser_roots"] += roots
    problems = _problems(record, True)
    assert any("other owners besides the successor" in p for p in problems), problems
    # A lifetime of the browser's parent pid born 5 ms after it is not its parent.
    late = [
        seen("process.start", "owner", 50, 1, 100.0),
        seen("process.start", "driver", 51, 50, 100.1),
        seen("process.start", "browser", 52, 51, 100.2),
        seen("process.start", "other", 51, 9, 100.205),
    ]
    assert harness.browser_lineage(late) == [[52, 100.2, None, 50, 100.0]]


def _browser_records(owner: int, pid: int, start: float) -> list[dict]:
    """The watcher's records of the driver an owner started and the browser
    root that driver started, as ``harness.browser_lineage`` reads them."""
    return [
        {
            "t": time.time(),
            "kind": "process.start",
            "actor": actor,
            "in_row": True,
            "pid": child,
            "ppid": parent,
            "start_identity": start,
        }
        for actor, child, parent in (
            ("driver", pid, owner),
            ("browser", pid + 1, pid),
        )
    ]


class _ModelledOwner:
    """An owner's process as the row holds it: running until *gone* says
    otherwise, never signalled."""

    def __init__(self, pid: int, gone: Callable[[], bool]) -> None:
        self.pid = pid
        self.gone = gone

    def status(self) -> str:
        if self.gone():
            raise psutil.NoSuchProcess(self.pid)
        return psutil.STATUS_RUNNING

    def is_running(self) -> bool:
        return not self.gone()

    def wait(self, timeout=None) -> None:
        if not self.gone():
            raise psutil.TimeoutExpired(timeout or 0, self.pid)

    def kill(self) -> None:
        raise AssertionError("a race signalled the owner")


class _TurnoverScene(_RaceScene):
    """A daemon row's modelled owner, asked to stand down by the row.

    The owner serves reads until it is asked; then it lets its admitted read
    finish, or cuts it when ``cuts`` and the drain runs out, and leaves. New
    work is refused with the frontend's own preflight line and served by a
    successor once the owner has gone; with ``runs_on_retiring`` it is run by
    the retiring owner instead.
    """

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.never_closes = True
        self.asked = threading.Event()
        self.gone = asyncio.Event()
        self.runs_on_retiring = False
        self.successor_start: float | None = None
        self.log_path = tmp_path / "daemon.log"
        self.log_path.write_text("INFO the owner is serving\n")
        monkeypatch.setattr(retirement_race, "AFTER_SECONDS", 0.1)
        monkeypatch.setattr(retirement_race, "TURNOVER_EXIT_SECONDS", 10.0)
        monkeypatch.setattr(
            retirement_race,
            "TURNOVER_CASES",
            {
                **TURNOVER_CASES,
                ROW_DRAIN: TURNOVER_CASES[ROW_DRAIN].__class__(first_release=0.3),
                ROW_REFUSED: TURNOVER_CASES[ROW_REFUSED].__class__(
                    first_release=0.3, second=AFTER
                ),
            },
        )
        scene = self
        published = {
            owner: SimpleNamespace(
                pid=pid,
                instance_id=owner,
                protocol_version=2,
                log_path=str(self.log_path),
            )
            for pid, owner in ((42, "first"), (43, "second"))
        }
        monkeypatch.setattr(
            harness.daemon_descriptor,
            "descriptor_path",
            lambda _root: tmp_path / "descriptor.json",
        )
        (tmp_path / "descriptor.json").write_text("{}")
        monkeypatch.setattr(
            harness.daemon_descriptor,
            "read",
            lambda _root: published[
                "second" if scene.successor_start is not None else "first"
            ],
        )
        self.owner = _ModelledOwner(42, self.gone.is_set)
        self.records: list[dict] = [_owner_record(42, 42.0)]
        monkeypatch.setattr(_Watcher, "records", self.records)
        auth_root = str(harness.claim_account(tmp_path).auth_root)

        def identify(*_a, **_k):
            if scene.successor_start is not None:
                return (
                    harness.OwnerIdentity(
                        43,
                        scene.successor_start,
                        "second",
                        auth_root,
                        _ModelledOwner(43, lambda: True),
                    ),
                    None,
                )
            return harness.OwnerIdentity(
                42, 42.0, "first", auth_root, scene.owner
            ), None

        monkeypatch.setattr(harness, "identify_owner", identify)

        def stand_down(_account, identified):
            assert identified is not None and identified.instance_id == "first"
            sent = time.monotonic_ns()
            scene.asked.set()
            with self.log_path.open("a") as log:
                log.write("INFO A newer build asked for the browser; standing down\n")
            return {
                "addressed": True,
                "sent_ns": sent,
                "answered_ns": time.monotonic_ns(),
                "status": 200,
                "standing_down": True,
                "error": None,
            }

        self.stand_downs = MagicMock(side_effect=stand_down)
        monkeypatch.setattr(harness, "ask_to_stand_down", self.stand_downs)

    async def _call(self, session, name: str, arguments: dict) -> dict:
        username = arguments.get("linkedin_username")
        if (
            isinstance(username, str)
            and username in SECOND_USERNAMES.values()
            and not self.runs_on_retiring
        ):
            return await self._recovered(session, name, username)
        try:
            return await super()._call(session, name, arguments)
        finally:
            if name == PERSON_TOOL and username == USERNAMES.get(self.row):
                # The owner's drain sees its admitted read finish, and it goes.
                self.background.append(asyncio.ensure_future(self._leave()))

    async def _recovered(self, session, name: str, username: str) -> dict:
        """New work as the frontend makes it: refused by the retiring owner's
        preflight, then elected and replayed once that owner has gone."""
        record: dict[str, Any] = {
            "tool": name,
            "began": time.time(),
            "began_monotonic_ns": time.monotonic_ns(),
        }
        session.calls.append(record)
        self.on_stderr(
            "INFO The shared browser owner refused the call preflight "
            "(HTTP 409, retiring)"
        )
        await self.gone.wait()
        self.successor_start = time.time()
        self.records.append(_owner_record(43, self.successor_start))
        self.on_stderr("INFO Attached to a replacement shared browser owner")
        self.on_stderr("INFO Attached to a replacement owner; running the call again")
        # A successor takes a while to start its browser.
        await asyncio.sleep(0.3)
        self.records += _browser_records(43, 4300, time.time())
        for section in self.pages:
            await asyncio.to_thread(self._read, person_path(username, section))
        record.update(
            ended=time.time(),
            ended_monotonic_ns=time.monotonic_ns(),
            outcome=RETURNED,
            is_error=False,
            read_the_post=False,
            marked_sections=list(self.pages),
            section_errors=[],
            text="",
        )
        return record

    async def _leave(self) -> None:
        while not self.asked.is_set():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.2)
        self.gone.set()

    async def host(self, *args, **kw):
        session = await super().host(*args, **kw)
        session.stderr.append("INFO Forwarding to the shared browser owner")
        return session

    async def run(self, row: str = ROW_DRAIN, **options):
        self.row = row
        try:
            return await measure_host_quit_row(
                profile=self.tmp_path / "auth" / "profile",
                experiment="K3",
                daemon=True,
                egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
                log=self.log,
                work_dir=self.tmp_path / "row",
                row=row,
                **options,
            )
        finally:
            await asyncio.gather(*self.background, return_exceptions=True)


@pytest.fixture
def turning(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _TurnoverScene(monkeypatch, tmp_path, profile, origin, certificates)


async def test_held_work_finishes_inside_the_drain_and_the_owner_then_leaves(turning):
    result = await turning.run(ROW_DRAIN)

    record = turning.published()["record"]
    assert record["problems"] == [], record["problems"]
    assert result.failures == [], result.failures
    # Asked once, of the owner the row identified.
    assert turning.stand_downs.call_count == 1
    assert record["stand_down"]["status"] == 200
    assert record["owner_exit"]["how"] == "exited"
    assert record["owner_log"] == {"turned_over": True, "cut": 0, "cut_lines": 0}
    assert _phases(turning) == [
        "armed",
        "entered",
        "stood down",
        "returned",
        "owner gone",
    ]


async def test_new_work_after_the_turnover_reaches_a_successor_and_passes(turning):
    result = await turning.run(ROW_REFUSED)

    record = turning.published()["record"]
    assert record["problems"] == [], record["problems"]
    assert result.failures == [], result.failures
    assert call_classification(record["second_attempts"]) == "retiring"
    assert record["owner_after"]["instance_id"] == "second"
    # The successor is the owner the row settles in the identified one's place.
    assert result.owner is not None and result.owner.get("replaced") is True
    attempts = [e for e in turning.log.records() if e["kind"] == "attempt"]
    assert [(e["attempt"], e["classification"]) for e in attempts] == [
        ("preflight", "retiring"),
        ("election", "attached"),
        ("replay", None),
    ]


async def test_new_work_run_on_the_retiring_owner_fails_the_row(turning):
    turning.runs_on_retiring = True
    result = await turning.run(ROW_REFUSED)

    problems = result.record["problems"]
    assert any("the new work ran on the retiring owner" in p for p in problems), (
        problems
    )
    assert any("the frontend reported no refusal of the call" in p for p in problems)
