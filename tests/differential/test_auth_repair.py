"""Auth repair and the login beside an owner: the staged sign-in, the
lineage, the judgement, the verdicts, the rows' wiring, the relay's boundary
and the model mapping.

No browser. The **sign-in** is the real synthetic origin over loopback TLS,
asked by a plain client that trusts the run's CA in its own context only.
The **lineage** reads real profiles on disk. The **judgement** is the real
``judge_row``. The **verdicts** start from an explicit valid record of each
cell and change one observation at a time, the plan's controls among them.
The **wiring** runs the real row entry in Direct mode, and the real scripts
on a modelled owner, with a product double whose requests are real requests
to the real origin: it meets the real wall, asks the real completion, and
writes a real session to disk. The **relay** is real sockets in this
process. The **model mapping** names tests that exist.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import http.client
import json
import os
import shutil
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from differential import auth_repair, harness, synthetic_origin
from differential.auth_repair import (
    AUTH_IDLE_TIMEOUT_SECONDS,
    BOUNDS,
    CASES,
    CLOSE_TOOL,
    COMPLETED,
    ENVIRONMENT,
    FAILED,
    K1_NOT_APPLICABLE,
    K2_NOT_APPLICABLE,
    MARKED_LINE,
    MODEL_COVERAGE,
    REPLAY_LINE,
    RESPONSE_LOSS_OPEN,
    ROW_COLD,
    ROW_FAILED,
    ROW_LOGIN,
    ROW_SECOND,
    WAITING,
    comparison_refusals,
    invalid_evidence,
    login_problems,
    marked,
    problems_for,
    repair_problems,
    repair_reading,
    semantic_differences,
    semantics,
)
from differential.call_loss import INVALID
from differential.events import EventLog
from differential.harness import (
    MUST_NOT_REPAIR,
    ORDINARY,
    PostQuit,
    compare_to_direct,
    judge_row,
    lifecycle_problems,
    measure_host_quit_row,
)
from differential.lease_probe import FREE
from differential.owner_hop_relay import MarkerDropRelay
from differential.profile_commands import (
    LOGIN_OPENED,
    PROFILE_SAVED,
    RETIRE_PROMPT,
    RETIRING_LINE,
)
from differential.session import (
    LINEAGE_LOST,
    LINEAGE_NONE,
    LINEAGE_REPLACED,
    LINEAGE_UNAUTHORIZED,
    LINEAGE_UNCERTAIN,
    LOGIN,
    LOST_AFTER_AUTHORIZATION,
    LOST_ANNOUNCED,
    LOST_SILENT,
    ORIGIN_REJECTED,
    REPLACED_AFTER_AUTHORIZATION,
    Authorization,
    ReplacementLineage,
    replacement_lineage,
    snapshot,
    synthetic_cookies,
)
from differential.synthetic_origin import (
    ISSUED_PREFIX,
    LOGIN_MARKER,
    LOGIN_PATH,
    LOGIN_POLL_PATH,
    LOGIN_REDIRECT,
    LOGIN_POLL_TIMEOUT_SECONDS,
    SyntheticOrigin,
    session_digest,
)
from differential.test_call_loss import (  # noqa: F401 - fixtures
    _CalibrationScene,
    _connect,
    _send,
    certificates,
    origin,
    owned,
)
from differential.test_preservation_gate import profile  # noqa: F401 - fixture
from differential.test_profile_commands import (
    _A,
    _call,
    _command,
    _feed,
    _functions,
    _host,
    _lifetime,
    _owner,
    _point,
)
from differential.test_row_judgement import _healthy
from linkedin_mcp_server.common_utils import secure_write_text
from linkedin_mcp_server.core.auth import _LOGIN_TITLE_PATTERNS, _is_auth_blocker_url
from linkedin_mcp_server.daemon_auth import MARKER_KEY
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    portable_cookie_path,
    source_state_path,
    write_source_state,
)

MS = 1_000_000

posix = pytest.mark.skipif(os.name == "nt", reason="a pseudo-terminal is POSIX's")


@pytest.fixture
def on_disk(profile):  # noqa: F811
    """The staged profile and its session (``test_preservation_gate``)."""
    return profile


@pytest.fixture
def served(origin):  # noqa: F811
    """The real synthetic origin (``test_call_loss``)."""
    return origin


@pytest.fixture
def ca(certificates):  # noqa: F811
    """The directory of the run's certificates (``test_call_loss``)."""
    return certificates


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    """The scripts' own waits, shortened: the doubles answer within a second,
    so a step that never comes is not waited for at the native bounds. The
    verdicts' bounds are untouched."""
    for name, seconds in (
        ("CLOSE_SECONDS", 5.0),
        ("LOGIN_START_SECONDS", 10.0),
        ("SECOND_MARK_SECONDS", 10.0),
        ("COMPLETION_SECONDS", 10.0),
        ("READ_SECONDS", 30.0),
        ("SECOND_SECONDS", 30.0),
    ):
        monkeypatch.setattr(auth_repair, name, seconds)


# --- The sign-in at the origin -------------------------------------------------------


def _exchange(
    served: SyntheticOrigin, ca: Path, path: str, cookie: str | None = None
) -> tuple[int, dict[str, str], bytes]:
    """One request to the real origin: its status, headers by lower-cased
    name, and body."""
    with _connect(served, ca) as connection:
        _send(connection, path, cookie)
        data = b""
        while chunk := connection.recv(65536):
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return int(lines[0].split(" ", 2)[1]), headers, body


def _issued_value(headers: dict[str, str]) -> str:
    return headers["set-cookie"].split(";", 1)[0].removeprefix("li_at=")


def test_a_healthy_origin_serves_no_wall_and_no_sign_in(served, ca, on_disk):
    _, staged = on_disk
    served.accept_session(staged.li_at)

    assert _exchange(served, ca, "/feed/", staged.li_at)[0] == 200
    assert _exchange(served, ca, LOGIN_PATH)[0] == 404
    assert _exchange(served, ca, LOGIN_POLL_PATH)[0] == 404
    record = served.login_record()
    assert record["armed"] is False and record["polls"] == 0
    first = served.requests[0]
    assert first.session_digests == (session_digest(staged.li_at),)
    assert not first.redirected and first.session_valid is True


def test_a_rejected_session_meets_the_wall_and_a_release_issues_a_fresh_one(
    served,
    ca,
    on_disk,
):
    _, staged = on_disk
    served.accept_session(staged.li_at)
    rejection = served.reject_sessions()
    assert rejection["digests"] == [session_digest(staged.li_at)]

    status, headers, _ = _exchange(served, ca, "/feed/", staged.li_at)
    assert (status, headers["location"]) == (302, LOGIN_REDIRECT)
    # The product's barrier check reads the redirect's path as a blocker.
    assert _is_auth_blocker_url(f"https://www.linkedin.com{LOGIN_REDIRECT}")
    # Without any session too: the wall answers whoever is not accepted.
    assert _exchange(served, ca, "/feed/")[0] == 302
    # A browser sent there is shown a wall that never asks for completion.
    status, _, redirected = _exchange(served, ca, LOGIN_REDIRECT)
    assert status == 200 and LOGIN_MARKER.encode() in redirected
    assert LOGIN_POLL_PATH.encode() not in redirected
    assert served.login_record()["walls"] == []
    # The wall the product's login opens asks.
    status, _, body = _exchange(served, ca, LOGIN_PATH)
    assert status == 200 and LOGIN_POLL_PATH.encode() in body
    assert len(served.login_record()["walls"]) == 1
    # The product's own barrier check reads both pages as a login wall.
    for page in (body, redirected):
        assert any(title in page.decode().lower() for title in _LOGIN_TITLE_PATTERNS)
    # Not released yet: pending, and answered at once rather than held.
    began = time.monotonic()
    assert _exchange(served, ca, LOGIN_POLL_PATH)[0] == 204
    assert time.monotonic() - began < LOGIN_POLL_TIMEOUT_SECONDS
    assert served.login_record()["issued"] == []

    served.release_login()
    status, headers, _ = _exchange(served, ca, LOGIN_POLL_PATH)
    assert status == 200
    value = _issued_value(headers)
    assert value.startswith(ISSUED_PREFIX) and value != staged.li_at
    cookie = headers["set-cookie"]
    assert "Domain=.linkedin.com" in cookie and "Max-Age=" in cookie
    assert {"secure", "httponly"} <= {
        part.strip().lower() for part in cookie.split(";")
    }
    record = served.login_record()
    [issued] = record["issued"]
    assert issued["digest"] == session_digest(value) and issued["phase"] == "row"
    assert issued["issued_ns"] > record["released_ns"]
    # By digest only: no value is anywhere in what the origin recorded.
    assert value not in json.dumps(record) and staged.li_at not in json.dumps(record)
    # The same browser asking again gets nothing new.
    assert _exchange(served, ca, LOGIN_POLL_PATH, value)[0] == 200
    assert len(served.login_record()["issued"]) == 1
    assert served.login_record()["answers"] == {
        "pending": 1,
        "issued": 1,
        "already": 1,
        "closed": 0,
    }
    # The fresh session reads; the rejected one still meets the wall.
    assert _exchange(served, ca, "/feed/", value)[0] == 200
    assert _exchange(served, ca, "/feed/", staged.li_at)[0] == 302
    assert served.accepts_digest(session_digest(value))
    assert not served.accepts_digest(session_digest(staged.li_at))
    walled = [
        request.session_digests for request in served.requests if request.redirected
    ]
    assert walled == [
        (session_digest(staged.li_at),),
        (),
        (session_digest(staged.li_at),),
    ]
    assert len(served.login_record()["redirects"]) == 3


def test_a_closed_sign_in_issues_nothing_even_once_released(
    served,
    ca,
    on_disk,
):
    _, staged = on_disk
    served.accept_session(staged.li_at)
    served.reject_sessions()
    served.release_login()
    served.close_login()

    assert _exchange(served, ca, LOGIN_POLL_PATH)[0] == 204
    record = served.login_record()
    assert record["issued"] == [] and record["answers"]["closed"] == 1


def test_a_sign_in_armed_without_a_rejection_leaves_the_staged_session_reading(
    served,
    ca,
    on_disk,
):
    _, staged = on_disk
    served.accept_session(staged.li_at)
    served.arm_login()

    assert _exchange(served, ca, "/feed/", staged.li_at)[0] == 200
    assert _exchange(served, ca, LOGIN_PATH)[0] == 200
    assert not any(request.redirected for request in served.requests)


def test_two_browsers_asking_after_one_release_are_both_issued_and_counted(
    served,
    ca,
    on_disk,
):
    # The origin counts what it issued; at most one is a verdict's to hold.
    _, staged = on_disk
    served.accept_session(staged.li_at)
    served.reject_sessions()
    served.release_login()
    first = _issued_value(_exchange(served, ca, LOGIN_POLL_PATH)[1])
    second = _issued_value(_exchange(served, ca, LOGIN_POLL_PATH)[1])

    assert first != second
    assert [item["ordinal"] for item in served.login_record()["issued"]] == [1, 2]


def test_each_sign_in_ask_is_bounded_below_the_navigation_and_relay_limits():
    # A lost answer meets this bound, never the 30 s ones a hold would.
    assert LOGIN_POLL_TIMEOUT_SECONDS < 30
    assert LOGIN_POLL_TIMEOUT_SECONDS < synthetic_origin._RELAY_IDLE_SECONDS


# --- The lineage, from real profiles -----------------------------------------------------


def _write_session(directory: Path, value: str) -> None:
    """A signed-in session with *value* as its ``li_at``, and a new generation."""
    cookies = synthetic_cookies(li_at=value)
    secure_write_text(
        portable_cookie_path(directory),
        json.dumps([cookie.to_playwright() for cookie in cookies]),
        mode=0o600,
    )
    write_source_state(directory)


def _rotate(directory: Path) -> Path:
    """What a sign-in does first: the session moved into quarantine."""
    quarantine = directory.parent / f"{QUARANTINE_PREFIX}{time.monotonic_ns()}"
    quarantine.mkdir()
    for path in (portable_cookie_path(directory), source_state_path(directory)):
        if path.exists():
            shutil.move(str(path), str(quarantine / path.name))
    return quarantine


def _restore(directory: Path, quarantine: Path) -> None:
    for path in quarantine.iterdir():
        shutil.move(str(path), str(directory.parent / path.name))
    quarantine.rmdir()


def _lineage(
    on_disk,
    *,
    authorize: bool = True,
    lost_first: bool = False,
    issued_early: bool = False,
    destroyed_first: bool = False,
    used: bool = True,
    replace: bool = True,
    stranger: bool = False,
) -> ReplacementLineage:
    directory, staged = on_disk
    before = snapshot(directory, expected_digest=staged.li_at_digest)
    if lost_first:
        # The original lost before anybody authorized anything.
        _write_session(directory, "synthetic-lost-first")
    at = time.monotonic_ns()
    authorization = (
        Authorization(
            ORIGIN_REJECTED,
            at,
            snapshot(directory, expected_digest=staged.li_at_digest),
        )
        if authorize
        else None
    )
    value = f"{ISSUED_PREFIX}replacement"
    issued_ns = at - 1 if issued_early else at + 10
    issued = []
    if destroyed_first:
        issued.append(
            {"digest": session_digest(f"{ISSUED_PREFIX}first"), "issued_ns": at + 5}
        )
    requests = []
    if replace:
        issued.append({"digest": session_digest(value), "issued_ns": issued_ns})
        _rotate(directory)
        _write_session(directory, value)
        if used:
            requests.append(
                {
                    "session_valid": True,
                    "session_digests": [session_digest(value)],
                    "monotonic_ns": issued_ns + 5,
                }
            )
    if stranger:
        _write_session(directory, "synthetic-nobody-issued")
    for item in issued:
        item.setdefault("phase", "row")
    after = snapshot(directory, expected_digest=staged.li_at_digest)
    return replacement_lineage(before, after, authorization, issued, requests)


def test_an_authorized_replacement_in_use_reads_as_replaced(on_disk):
    lineage = _lineage(on_disk)

    assert lineage.reading == LINEAGE_REPLACED and lineage.problems == ()
    assert lineage.replacement is not None
    assert lineage.replacement["digest"] == session_digest(
        f"{ISSUED_PREFIX}replacement"
    )
    assert lineage.replacement["phase"] == "row"
    assert lineage.replacement["used_ns"] is not None
    # A digest and a generation, never the value.
    assert f"{ISSUED_PREFIX}replacement" not in json.dumps(lineage.as_record())


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param(
            {"authorize": False},
            "no authorization that covers a replacement was recorded",
            id="unauthorized-replacement",
        ),
        pytest.param(
            {"lost_first": True},
            "the original generation was already lost when the authorization was "
            "recorded",
            id="replacement-erasing-an-earlier-loss",
        ),
        pytest.param(
            {"issued_early": True},
            "1 session(s) were issued before the authorization",
            id="issued-before-the-authorization",
        ),
        pytest.param(
            {"destroyed_first": True},
            "1 session(s) the origin issued are no longer on disk",
            id="second-repair-destroying-a-fresh-generation",
        ),
        pytest.param(
            {"replace": False, "stranger": True},
            "a session on disk is neither the staged one nor issued",
            id="a-session-nobody-issued",
        ),
    ],
)
def test_a_replacement_the_authorization_does_not_cover_is_unauthorized(
    on_disk,
    change,
    problem,
):
    lineage = _lineage(on_disk, **change)

    assert lineage.reading == LINEAGE_UNAUTHORIZED
    assert any(problem in found for found in lineage.problems), lineage.problems


def test_a_replacement_never_seen_in_use_is_uncertain(on_disk):
    lineage = _lineage(on_disk, used=False)

    assert lineage.reading == LINEAGE_UNCERTAIN
    assert lineage.problems == (
        "no request after the issue carried the replacement and was accepted",
    )


def test_an_authorized_loss_with_nothing_issued_reads_as_lost(on_disk):
    directory, _ = on_disk
    lineage = _lineage(on_disk, replace=False)

    # The original is what is on disk: a failed login put it back.
    assert lineage.reading == LINEAGE_LOST and lineage.problems == ()
    assert portable_cookie_path(directory).exists()


def test_nothing_authorized_and_nothing_issued_reads_as_none(on_disk):
    assert _lineage(on_disk, authorize=False, replace=False).reading == LINEAGE_NONE


def test_a_replacement_without_a_new_generation_is_uncertain(on_disk):
    directory, staged = on_disk
    before = snapshot(directory, expected_digest=staged.li_at_digest)
    at = time.monotonic_ns()
    authorization = Authorization(
        ORIGIN_REJECTED, at, snapshot(directory, expected_digest=staged.li_at_digest)
    )
    value = f"{ISSUED_PREFIX}replacement"
    # The cookie replaced in place, no new generation written beside it.
    cookies = synthetic_cookies(li_at=value)
    secure_write_text(
        portable_cookie_path(directory),
        json.dumps([cookie.to_playwright() for cookie in cookies]),
        mode=0o600,
    )
    after = snapshot(directory, expected_digest=staged.li_at_digest)
    lineage = replacement_lineage(
        before,
        after,
        authorization,
        [{"digest": session_digest(value), "issued_ns": at + 10, "phase": "row"}],
        [
            {
                "session_valid": True,
                "session_digests": [session_digest(value)],
                "monotonic_ns": at + 20,
            }
        ],
    )

    assert lineage.reading == LINEAGE_UNCERTAIN
    assert lineage.problems == ("the replacement is on disk without a new generation",)


def test_a_reading_that_fails_leaves_the_lineage_uncertain(on_disk):
    directory, staged = on_disk
    before = snapshot(directory, expected_digest=staged.li_at_digest)
    portable_cookie_path(directory).write_text("{not json")
    after = snapshot(directory, expected_digest=staged.li_at_digest)

    lineage = replacement_lineage(before, after, None, [], [])

    assert lineage.reading == LINEAGE_UNCERTAIN
    assert lineage.problems == ("a reading of the session failed",)


# --- The judgement -----------------------------------------------------------------------


def _replaced(on_disk, *, lineage: str, notice: bool = True, **changes):
    """A row whose session the rejection retired and a sign-in replaced."""
    directory, staged = on_disk
    observed = _healthy(on_disk, daemon=False)
    _rotate(directory)
    _write_session(directory, f"{ISSUED_PREFIX}replacement")
    after = snapshot(directory, expected_digest=staged.li_at_digest)
    host = dataclasses.replace(observed.host)
    host.user_lines = (
        ["Session expired. A login browser window has been opened."] if notice else []
    )
    fields: dict[str, Any] = {
        "host": host,
        "after": after,
        "post_quit": PostQuit(valid=True, withheld=MUST_NOT_REPAIR, origin_judged=True),
        "expect_session": REPLACED_AFTER_AUTHORIZATION,
        "authorized": ORIGIN_REJECTED,
        "lineage": ReplacementLineage(lineage),
    }
    return dataclasses.replace(observed, **{**fields, **changes})


def test_an_authorized_replacement_meets_its_expectation_beside_the_lost_original(
    on_disk,
):
    vector, failures = judge_row(_replaced(on_disk, lineage=LINEAGE_REPLACED))

    assert failures == []
    # The original generation's outcome is kept, the lineage beside it.
    assert vector.o4_session == LOST_ANNOUNCED
    assert vector.o4_lineage == LINEAGE_REPLACED
    assert set(vector.o3_authorized) and vector.o3_protected == ()


def test_an_unauthorized_replacement_fails_and_authorizes_no_change(on_disk):
    vector, failures = judge_row(_replaced(on_disk, lineage=LINEAGE_UNAUTHORIZED))

    assert (
        "O4: the session was lost or replaced without an authorization recorded "
        "before it" in failures
    )
    assert any("not replaced-after-authorization" in f for f in failures), failures
    # Nothing it changed is covered any more.
    assert vector.o3_authorized == () and set(vector.o3_protected)


def test_a_loss_expectation_is_not_met_by_a_replacement(on_disk):
    _, failures = judge_row(
        _replaced(
            on_disk,
            lineage=LINEAGE_REPLACED,
            expect_session=LOST_AFTER_AUTHORIZATION,
        )
    )

    assert any("not lost-after-authorization" in f for f in failures), failures


def test_a_preservation_only_notice_is_no_announcement_to_the_caller(on_disk):
    # The notice reached the probe after the row, never the row's caller.
    observed = _replaced(on_disk, lineage=LINEAGE_REPLACED, notice=False)
    observed = dataclasses.replace(
        observed,
        post_quit=dataclasses.replace(
            observed.post_quit, user_lines=["Run with --login to sign in again"]
        ),
    )
    vector, failures = judge_row(observed)

    assert vector.o4_session == LOST_SILENT
    assert failures == []


def test_a_differing_lineage_is_a_difference_from_direct(on_disk):
    direct, _ = judge_row(_replaced(on_disk, lineage=LINEAGE_REPLACED))
    daemon = dataclasses.replace(direct, o4_lineage=LINEAGE_UNAUTHORIZED)

    assert compare_to_direct(direct, daemon) == [
        "o4_lineage: Direct 'replaced', daemon 'unauthorized'"
    ]


# --- Verdicts: records ---------------------------------------------------------------

#: The staged session's digest, and the fresh one's.
STAGED = "a" * 64
FRESH = "b" * 64


def _request(path: str, ms: int, *, digests=(), valid=True, redirected=False) -> dict:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "session_valid": valid,
        "t": 1000.0 + ms / 1000,
        "monotonic_ns": ms * MS,
        "session_digests": list(digests),
        "redirected": redirected,
    }


def _shot(ms: int, *, generation: str = "g0") -> dict:
    return {"generation": generation, "seen_ns": ms * MS}


def _sign_in(*, released: int | None, issued: list[int], last_poll: int) -> dict:
    return {
        "armed": True,
        "armed_ns": 50 * MS,
        "rejections": [{"digests": [STAGED], "monotonic_ns": 1_300 * MS}],
        "walls": [1_600 * MS],
        "redirects": [1_500 * MS],
        "polls": 20,
        "first_poll_ns": 1_700 * MS,
        "last_poll_ns": last_poll * MS,
        "last_pending_ns": (released or last_poll) * MS,
        "answers": {"pending": 19, "issued": len(issued), "already": 0, "closed": 0},
        "released_ns": released * MS if released is not None else None,
        "released_by": "row" if released is not None else None,
        "released_phase": "row" if released is not None else None,
        "closed_ns": 20_000 * MS,
        "issued": [
            {"digest": FRESH, "issued_ns": at * MS, "phase": "row", "ordinal": n + 1}
            for n, at in enumerate(issued)
        ],
    }


def _base(row: str, *, daemon: bool) -> dict:
    record: dict[str, Any] = {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": AUTH_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "environment": dict(ENVIRONMENT),
        "bounds": dict(BOUNDS),
        "observation_problems": [],
        "script_error": None,
        "host": _host(),
        "egress": {"forwarded": ["www.linkedin.com"], "refused": []},
        "calls": [_feed(100, 900)],
        "requests": [_request("/feed/", 500, digests=[STAGED])],
        "owner_processes": [_lifetime(*_A[:2])] if daemon else [],
        "gate_processes": [],
    }
    if daemon:
        record["owner_identified"] = list(_A)
    return record


def _repair(row: str = ROW_COLD, *, daemon: bool = True) -> dict:
    """H-R16. The browser closed at 1 s, gone at 1.2 s; the session rejected
    at 1.3 s; the cold read sent at 1.4 s, its stale feed walled at 1.5 s,
    the wall at 1.6 s and the first ask at 1.7 s. Released at 2.5 s and
    issued at 3 s, the generation on disk at 6 s and the login's browser
    gone at 7 s. K3: the replay read the fresh feed at 8 s and the read
    ended at 9 s. K1: Direct answered at 2 s, the host read again from 7.5
    s. Failed: never released; the browser gone at 9 s, the last ask at 8 s.
    """
    failed = row == ROW_FAILED
    record = _base(row, daemon=daemon)
    record.update(bounds=dict(BOUNDS), release=not failed)
    record["calls"].append(
        _call(CLOSE_TOOL, 1_000, 1_100, is_error=False, read_the_post=False)
    )
    record["browser_closed"] = {"remaining": [], "seen_ns": 1_200 * MS}
    record["rejection"] = {
        "digests": [STAGED],
        "monotonic_ns": 1_300 * MS,
        "snapshot": _shot(1_310),
    }
    record["authorized"] = ORIGIN_REJECTED
    record["authorization"] = {
        "kind": ORIGIN_REJECTED,
        "at_ns": 1_300 * MS,
        "snapshot": _shot(1_310),
    }
    record["first_ask_ns"] = 1_700 * MS
    record["requests"] += [
        _request("/feed/", 1_500, digests=[STAGED], valid=False, redirected=True),
        _request(LOGIN_PATH, 1_600, digests=[STAGED], valid=False),
    ]
    if failed:
        cold = _call(
            harness.READ_TOOL,
            1_400,
            8_500 if daemon else 2_000,
            is_error=True,
            read_the_post=False,
        )
        record["calls"].append(cold)
        record["login"] = _sign_in(released=None, issued=[], last_poll=8_000)
        record["failed_settlement"] = {"remaining": [], "seen_ns": 9_000 * MS}
        record["failed_login"] = copy.deepcopy(record["login"])
        record["host_lines"] = {
            "replayed": 0,
            "not_replayed": 1 if daemon else 0,
            "signed_in": 0,
            "peer": 0,
        }
    else:
        record["release"] = {"released_ns": 2_500 * MS, "released_by": "row"}
        record["login"] = _sign_in(released=2_500, issued=[3_000], last_poll=3_000)
        completion: dict[str, Any] = {
            "issued_seen_ns": 3_050 * MS,
            "generation_seen_ns": 6_000 * MS,
        }
        if not daemon:
            completion["browser_gone"] = {"remaining": [], "seen_ns": 7_000 * MS}
        record["completion"] = completion
        if daemon:
            record["calls"].append(_feed(1_400, 9_000))
            record["requests"].append(_request("/feed/", 8_000, digests=[FRESH]))
            record["host_lines"] = {
                "replayed": 1,
                "not_replayed": 0,
                "signed_in": 1,
                "peer": 0,
            }
        else:
            record["calls"].append(
                _call(
                    harness.READ_TOOL, 1_400, 2_000, is_error=True, read_the_post=False
                )
            )
            record["calls"].append(_feed(7_500, 8_500))
            record["requests"].append(_request("/feed/", 8_000, digests=[FRESH]))
            record["host_lines"] = {
                "replayed": 0,
                "not_replayed": 0,
                "signed_in": 0,
                "peer": 0,
            }
    record["read_open"] = False
    if daemon:
        marks = 2 if row == ROW_SECOND else 1
        record["marked"] = [{"reason": "stale", "replayable": True}] * marks
        record["marks"] = marks
        record["owner_after"] = _owner(9_500)
    if row == ROW_SECOND:
        record["second_marked_ns"] = 2_400 * MS
        record["second"] = {
            "made": True,
            "launched_ns": 1_800 * MS,
            "call": _feed(2_000, 9_500),
            "forwarded": True,
            "quit_problems": [],
            "lines": {"replayed": 1, "not_replayed": 0, "signed_in": 1, "peer": 1},
            "retained": False,
        }
    return record


def _login(*, daemon: bool = True) -> dict:
    """H-R10a-login. The host quits at 1 s; the login authorized at 1.6 s and
    started at 2 s. Daemon: asked to retire at 3 s, a checkpoint from 3.1 to
    3.3 s, answered at 3.4 s, the owner gone at 3.8 s and the login retiring
    at 3.5 s. The browser opened at 4 s, the first ask at 5 s, released at
    5.1 s, issued at 5.2 s; saved at 8.8 s, exit at 9 s."""
    record = _base(ROW_LOGIN, daemon=daemon)
    record.update(command=["--login"], terminal=True, bounds=dict(BOUNDS))
    record["host_quit_ns"] = 1_000 * MS
    record["authorized"] = LOGIN
    record["authorization"] = {
        "kind": LOGIN,
        "at_ns": 1_600 * MS,
        "snapshot": _shot(1_610),
    }
    login = _sign_in(released=5_100, issued=[5_200], last_poll=5_200)
    login.update(
        rejections=[], redirects=[], walls=[4_500 * MS], first_poll_ns=5_000 * MS
    )
    record["login"] = login
    record["release"] = {"released_ns": 5_100 * MS, "released_by": "row"}
    record["first_ask_ns"] = 5_000 * MS
    lines = [
        (4_000, f"{LOGIN_OPENED}..."),
        (8_800, f"{PROFILE_SAVED} /tmp/profile"),
    ]
    if not daemon:
        record["settlement"] = {
            "remaining": [],
            "unresolved": [],
            "lease": FREE,
            "seen_ns": 1_500 * MS,
        }
        record["login_command"] = _command(
            "login", ["--login"], lines, started=2_000, exited=9_000
        )
        return record
    record["owner_after_quit"] = _owner(1_500)
    record["before_answer"] = _point(3_100, 3_300)
    record["login_command"] = _command(
        "login",
        ["--login"],
        [(3_000, RETIRE_PROMPT + "y"), (3_500, f"ℹ️  {RETIRING_LINE}"), *lines],
        started=2_000,
        exited=9_000,
        expected=[(RETIRE_PROMPT, 3_000)],
        answers=[("y", 3_400)],
    )
    record["owner_exit"] = {"how": "exited", "seen_ns": 3_800 * MS}
    record["owner_lines"] = {"standing_down": 1, "idle_exit": 0}
    return record


def _findings(problems: list[str]) -> list[str]:
    return [problem for problem in problems if not problem.startswith(INVALID)]


_VALID = [
    pytest.param(lambda: _repair(ROW_COLD), True, id="cold-K3"),
    pytest.param(lambda: _repair(ROW_COLD, daemon=False), False, id="cold-K1"),
    pytest.param(lambda: _repair(ROW_SECOND), True, id="second-K3"),
    pytest.param(lambda: _repair(ROW_FAILED), True, id="failed-K3"),
    pytest.param(lambda: _repair(ROW_FAILED, daemon=False), False, id="failed-K1"),
    pytest.param(lambda: _login(), True, id="login-K3"),
    pytest.param(lambda: _login(daemon=False), False, id="login-K1"),
]


@pytest.mark.parametrize(("make", "daemon"), _VALID)
def test_each_valid_record_has_no_problem(make, daemon):
    record = make()

    assert problems_for(record, daemon=daemon) == []
    # Read again from the packet, the verdict is the same.
    assert problems_for(json.loads(json.dumps(record)), daemon=daemon) == []


def test_the_second_frontend_has_no_direct_column():
    problems = repair_problems(_repair(ROW_SECOND, daemon=False), daemon=False)

    assert (
        problems[-1]
        == f"{ROW_SECOND} has no Direct column: {K1_NOT_APPLICABLE['reason']}"
    )


def test_a_replay_of_a_call_the_owner_marked_not_replayable_fails():
    record = _repair()
    record["marked"] = [{"reason": "stale", "replayable": False}]

    assert _findings(repair_problems(record, daemon=True)) == [
        "a call the owner marked not replayable was run again"
    ]


def test_a_browser_reading_on_the_stale_generation_during_the_repair_fails():
    record = _repair(ROW_SECOND)
    # The owner opened a browser on the session that failed while the
    # first frontend's login waited.
    record["requests"].append(
        _request("/feed/", 2_000, digests=[STAGED], valid=False, redirected=True)
    )

    assert _findings(repair_problems(record, daemon=True)) == [
        "a browser read on the stale generation during the repair: 1 /feed/ "
        "request(s) after the login waited"
    ]


def test_a_second_repair_establishing_another_session_fails():
    record = _repair(ROW_SECOND)
    record["login"]["issued"].append(
        {"digest": "c" * 64, "issued_ns": 4_000 * MS, "phase": "row", "ordinal": 2}
    )

    assert _findings(repair_problems(record, daemon=True)) == [
        "2 fresh sessions were established, not at most one"
    ]


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            {"remaining": [777], "seen_ns": 130_000 * MS},
            "the failed login did not settle within its budget: its browser was "
            "still there 120.0s after its wall",
            id="still-there",
        ),
        pytest.param(
            {"remaining": [], "seen_ns": 130_000 * MS},
            "the failed login settled 128.4s after its wall, beyond 120.0s",
            id="too-late",
        ),
        pytest.param(
            {"remaining": [], "seen_ns": 7_000 * MS},
            "the failed login still asked after its browser was gone",
            id="asking-after",
        ),
    ],
)
def test_a_failed_login_that_does_not_settle_inside_its_budget_fails(change, finding):
    record = _repair(ROW_FAILED)
    record["failed_settlement"] = change

    assert _findings(repair_problems(record, daemon=True)) == [finding]


@pytest.mark.parametrize("daemon", [True, False])
def test_a_failed_login_answered_with_the_post_fails(daemon):
    record = _repair(ROW_FAILED, daemon=daemon)
    reads = [c for c in record["calls"] if c["tool"] == harness.READ_TOOL]
    reads[-1].update(is_error=False, read_the_post=True)

    assert "the cold read returned the post although the login failed" in (
        _findings(repair_problems(record, daemon=daemon))
    )


_REPAIR_OBSERVATIONS = [
    pytest.param(
        lambda r: r["host_lines"].update(replayed=2),
        "the frontend did not run the read again exactly once: 2",
        id="replayed-twice",
    ),
    pytest.param(
        lambda r: r["calls"][-1].update(is_error=True, read_the_post=False),
        "the cold read was not answered after the sign-in: outcome 'returned', "
        "error True",
        id="not-answered",
    ),
    pytest.param(
        lambda r: r.update(marks=2, marked=r["marked"] * 2),
        "the owner marked 2 failure(s), not 1",
        id="marked-twice",
    ),
    pytest.param(
        lambda r: r["marked"].__setitem__(0, {"reason": "missing", "replayable": True}),
        "the owner did not mark the cold read's failure as a stale session: "
        "['missing']",
        id="not-stale",
    ),
    pytest.param(
        lambda r: r["completion"].update(generation_seen_ns=None),
        "the login did not write a new generation after its release",
        id="no-generation",
    ),
    pytest.param(
        lambda r: r["login"].update(issued=[]),
        "no fresh session was issued after the release",
        id="nothing-issued",
    ),
    pytest.param(
        lambda r: r["owner_processes"].append(_lifetime(5151, 2000.0)),
        "the row launched another owner beside the identified one",
        id="another-owner",
    ),
]


@pytest.mark.parametrize(("change", "finding"), _REPAIR_OBSERVATIONS)
def test_the_cold_repair_holds_each_of_its_observations(change, finding):
    record = _repair()
    change(record)

    assert _findings(repair_problems(record, daemon=True)) == [finding]


_INVALID = [
    pytest.param(
        lambda r: r["browser_closed"].update(remaining=[999]),
        "the warm browser was not shown gone",
        id="browser-not-closed",
    ),
    pytest.param(
        lambda r: r["requests"].__setitem__(
            1, {**r["requests"][1], "redirected": False}
        ),
        "the stale session never met the wall",
        id="no-wall",
    ),
    pytest.param(
        lambda r: r["login"].update(first_poll_ns=None),
        "the login never asked for its completion",
        id="never-asked",
    ),
    pytest.param(
        lambda r: r["login"].update(released_ns=1_650 * MS),
        "the completion was released before the login asked",
        id="released-early",
    ),
    pytest.param(
        lambda r: r["authorization"].update(at_ns=1_400 * MS),
        "the origin-rejected authorization was not recorded first",
        id="authorized-late",
    ),
    pytest.param(
        lambda r: r.update(environment={**ENVIRONMENT, "LOGIN_TIMEOUT": "1800"}),
        "the cell did not run with its declared bounds",
        id="other-bounds",
    ),
    pytest.param(
        lambda r: r["login"].update(closed_ns=None),
        "the sign-in was not closed by the teardown",
        id="left-open",
    ),
]


@pytest.mark.parametrize(("change", "invalid"), _INVALID)
def test_missing_evidence_is_invalid_and_never_a_finding(change, invalid):
    record = _repair()
    change(record)
    problems = repair_problems(record, daemon=True)

    assert any(invalid in problem for problem in invalid_evidence(problems)), problems
    assert _findings(problems) == []


def test_direct_must_answer_the_cold_read_with_a_started_login_and_then_read():
    record = _repair(daemon=False)
    record["calls"][2].update(is_error=False, read_the_post=True)
    assert _findings(repair_problems(record, daemon=False)) == [
        "Direct did not answer the cold read with a started login: outcome "
        "'returned', error False"
    ]
    record = _repair(daemon=False)
    record["calls"][3].update(began_monotonic_ns=5_000 * MS)
    assert invalid_evidence(repair_problems(record, daemon=False)) == [
        f"{INVALID}the read again was sent before the login completed"
    ]


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        pytest.param(
            lambda r: r.update(second_marked_ns=None),
            f"{INVALID}the second frontend was not shown to meet the repair before "
            f"the release",
            id="not-during-the-repair",
        ),
        pytest.param(
            lambda r: r["second"]["call"].update(is_error=True, read_the_post=False),
            "the second host's read did not end on the new generation: outcome "
            "'returned', error True",
            id="second-read-failed",
        ),
        pytest.param(
            lambda r: r["second"].update(forwarded=False),
            "the second host's call was not forwarded to the owner",
            id="second-not-forwarded",
        ),
    ],
)
def test_the_second_frontend_holds_each_of_its_observations(change, problem):
    record = _repair(ROW_SECOND)
    change(record)

    assert problem in repair_problems(record, daemon=True)


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            lambda r: r["login_command"].update(returncode=1),
            "the login did not save the new session: exit 1",
            id="not-saved",
        ),
        pytest.param(
            lambda r: r["login"].update(first_poll_ns=3_200 * MS),
            "the login asked for its completion before the owner retired",
            id="asked-before-the-answer",
        ),
        pytest.param(
            lambda r: r["owner_lines"].update(standing_down=0),
            "the owner's log does not say once that a profile command asked: 0",
            id="no-stand-down",
        ),
        pytest.param(
            lambda r: r["owner_exit"].update(how="still running"),
            "the owner is not shown to exit after the confirmed retirement: "
            "'still running'",
            id="owner-stayed",
        ),
    ],
)
def test_the_login_beside_an_owner_holds_each_of_its_observations(change, finding):
    record = _login()
    change(record)

    assert _findings(login_problems(record, daemon=True)) == [finding]


def test_a_login_authorized_after_it_started_is_invalid():
    record = _login(daemon=False)
    record["authorization"]["at_ns"] = 2_500 * MS

    assert invalid_evidence(login_problems(record, daemon=False)) == [
        f"{INVALID}the login authorization was not recorded first"
    ]


def test_the_three_ends_of_a_login_are_kept_apart():
    # Completion: issued before the cold read ended.
    assert repair_reading(_repair())["at_read_end"] == COMPLETED
    # The frontend's wait ran out while the login still asked.
    waiting = _repair(ROW_FAILED)
    waiting["calls"][-1]["ended_monotonic_ns"] = 5_000 * MS
    assert repair_reading(waiting) == {"at_read_end": WAITING, "settled_failed": True}
    # The login itself failed before the read ended, and settled.
    failed = _repair(ROW_FAILED)
    assert repair_reading(failed) == {"at_read_end": FAILED, "settled_failed": True}
    # Not settled: its browser still there.
    failed["failed_settlement"]["remaining"] = [777]
    assert repair_reading(failed)["settled_failed"] is False


def test_the_owner_s_marks_are_read_with_what_it_marked():
    lines = [
        "INFO Asking the client to sign in (stale, replayable=True)",
        "INFO something else",
        "INFO Asking the client to sign in (missing, replayable=False)",
    ]

    assert marked(lines) == [
        {"reason": "stale", "replayable": True},
        {"reason": "missing", "replayable": False},
    ]
    assert marked([f"{MARKED_LINE} in a shape this build does not know"]) == [
        {"reason": None, "replayable": None}
    ]


def test_a_repeat_reads_as_its_reference_and_an_invalid_one_is_refused():
    assert semantic_differences(_repair(), _repair()) == []
    replayed_twice = _repair()
    replayed_twice["host_lines"]["replayed"] = 2
    assert semantic_differences(_repair(), replayed_twice)
    assert semantic_differences(_repair(), None)
    assert semantics(_repair(ROW_SECOND))["second"] is True


def test_the_sign_in_events_carry_digests_and_never_a_value(tmp_path):
    log = EventLog(tmp_path, run="r")

    def rejected(digests: list[str]) -> None:
        log.emit(
            experiment="K3",
            row=ROW_COLD,
            actor="origin",
            kind="login.rejected",
            digests=digests,
            monotonic_ns=1,
        )

    rejected([STAGED])
    # Too short to be a digest, and not hexadecimal: a value either way.
    for value in ("deadbeef", "z" * 64):
        with pytest.raises(ValueError, match="digests is invalid"):
            rejected([value])
    with pytest.raises(ValueError, match="reading is invalid"):
        log.emit(
            experiment="K3",
            row=ROW_COLD,
            actor="harness",
            kind="r17.lineage",
            reading="replaced-somehow",
        )


def test_k3_is_held_to_k1_only_from_two_valid_records():
    assert comparison_refusals(_repair(daemon=False), _repair()) == []
    assert comparison_refusals(None, _repair())
    invalid = _login()
    invalid["k2"] = None
    assert comparison_refusals(_login(daemon=False), invalid)


# --- Declarations ------------------------------------------------------------------------


def test_every_row_here_is_declared_with_its_bounds_and_a_policy_that_cannot_repair():
    for row, case in CASES.items():
        lifecycle = harness.ROWS[row]
        assert lifecycle.auth and lifecycle.recorded and not lifecycle.scenarios
        assert lifecycle.idle_timeout == AUTH_IDLE_TIMEOUT_SECONDS
        assert lifecycle.expect_session == case.expect_session
        assert lifecycle.preservation == MUST_NOT_REPAIR
        assert lifecycle.environment == ENVIRONMENT
        assert lifecycle.commands is case.commands
        assert harness.ROW_VERDICTS[row] is problems_for
        assert lifecycle_problems(row, lifecycle) == []
    assert {
        r for r, c in CASES.items() if c.expect_session == LOST_AFTER_AUTHORIZATION
    } == {ROW_FAILED}


def test_a_lineage_expectation_needs_a_sign_in_and_a_policy_that_cannot_repair():
    declared = harness.ROWS[ROW_COLD]

    assert lifecycle_problems(
        ROW_COLD, dataclasses.replace(declared, preservation=ORDINARY)
    ) == [
        "a replaced or failed-login session left to a preservation session that "
        "can repair it"
    ]
    assert lifecycle_problems(ROW_COLD, dataclasses.replace(declared, auth=False)) == [
        "a lineage expectation with no sign-in to read it from"
    ]
    assert "a sign-in that combines with other scenarios" in lifecycle_problems(
        ROW_COLD, dataclasses.replace(declared, scenarios=True)
    )
    assert lifecycle_problems(
        ROW_COLD, dataclasses.replace(declared, expect_session=LOST_ANNOUNCED)
    ) == [f"an expected session of {LOST_ANNOUNCED!r}"]


# --- What stays with the models ----------------------------------------------------------


@pytest.mark.parametrize(
    "branch",
    [
        pytest.param(
            branch,
            marks=pytest.mark.differential_row(row=row, experiment="K3", column="unit"),
        )
        for branch, (row, _) in MODEL_COVERAGE.items()
    ],
)
def test_each_branch_left_to_the_models_names_tests_that_exist(branch):
    _row, nodes = MODEL_COVERAGE[branch]
    root = Path(__file__).resolve().parents[2]
    assert nodes
    for node in nodes:
        path, _, name = node.partition("::")
        assert name in _functions(root / path), node


def test_the_response_loss_lane_is_labelled_open_and_its_models_as_latch_models():
    [branch] = [name for name in MODEL_COVERAGE if name.startswith("response loss")]

    assert "latch models, not response-loss tests" in branch
    assert "stays open" in RESPONSE_LOSS_OPEN
    assert all(
        node.startswith("tests/test_bootstrap.py::TestTheOwnerStaysQuiescent")
        for node in MODEL_COVERAGE[branch][1]
    )


# --- The relay's boundary, process-free -----------------------------------------------------

TOKEN = "synthetic-bearer-7d1e"


class _Owner(BaseHTTPRequestHandler):
    """An owner hop's stand-in: a marked answer, a marked answer in chunks
    split through the marker, or a plain one, by the request's body."""

    protocol_version = "HTTP/1.1"
    server: Any

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.arrived.append(
            (body, self.headers.get("Authorization") == f"Bearer {TOKEN}")
        )
        if body in (b"marker", b"marker-chunked"):
            payload = json.dumps({"result": {"_meta": {MARKER_KEY: {"v": 2}}}}).encode()
        else:
            payload = json.dumps({"result": {"content": body.decode()}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if body.endswith(b"chunked"):
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            key = MARKER_KEY.encode()
            # Through the marker where there is one, so no chunk holds it whole.
            middle = payload.index(key) + 4 if key in payload else len(payload) // 2
            for part in (payload[:middle], payload[middle:]):
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture
def owner_hop():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Owner)
    server.daemon_threads = True
    cast(Any, server).arrived = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    relay = MarkerDropRelay(("127.0.0.1", server.server_address[1])).start()
    try:
        yield server, relay
    finally:
        relay.stop()
        server.shutdown()
        server.server_close()


def _raw(port: int, body: bytes) -> bytes:
    """One request on a raw socket, and every byte that came back."""
    with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
        connection.sendall(
            b"POST /mcp?session=x HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            + f"Authorization: Bearer {TOKEN}\r\n".encode()
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        data = b""
        while chunk := connection.recv(65536):
            data += chunk
    return data


def _body(answer: bytes) -> bytes:
    return answer.partition(b"\r\n\r\n")[2]


def _data(answer: bytes) -> bytes:
    """An answer's body with any chunking taken off."""
    head, _, body = answer.partition(b"\r\n\r\n")
    if b"chunked" not in head.lower():
        return body
    data = b""
    while True:
        size_line, _, body = body.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            return data
        data, body = data + body[:size], body[size + 2 :]


def test_the_relay_forwards_every_other_exchange_byte_for_byte(owner_hop):
    server, relay = owner_hop
    direct = _raw(server.server_address[1], b"plain")
    relayed = _raw(relay.port, b"plain")
    assert (
        _body(relayed) == _body(direct)
        and relayed.split(b"\r\n", 1)[0] == (direct.split(b"\r\n", 1)[0])
    )
    # A chunked answer goes on in its own chunks.
    chunked = _raw(relay.port, b"plain-chunked")
    assert _body(chunked) == _body(_raw(server.server_address[1], b"plain-chunked"))
    assert b"plain-chunked" in _data(chunked)
    # Several exchanges on one kept-alive connection, each answered.
    connection = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=10)
    for body in (b"one", b"two"):
        connection.request("POST", "/mcp", body, {"Authorization": f"Bearer {TOKEN}"})
        assert body in connection.getresponse().read()
    connection.close()
    assert [e["forwarded"] for e in relay.exchanges] == [True] * 4


@pytest.mark.parametrize("body", [b"marker", b"marker-chunked"])
def test_the_relay_drops_exactly_the_first_marked_answer_after_it_arrived(
    owner_hop, body
):
    server, relay = owner_hop

    # Not one byte of the marked answer: the connection simply ends.
    assert _raw(relay.port, body) == b""
    # The upstream had the request, and the relay read its whole answer.
    assert server.arrived == [(body, True)]
    [dropped] = relay.exchanges
    assert dropped["marked"] is True and dropped["forwarded"] is False
    assert dropped["status"] == 200 and dropped["length"] > 0
    # Only the first: the next marked answer goes through.
    assert MARKER_KEY.encode() in _data(_raw(relay.port, body))
    assert [e["forwarded"] for e in relay.exchanges] == [False, True]


def test_the_relay_records_no_token_query_or_body(owner_hop):
    _, relay = owner_hop
    _raw(relay.port, b"marker")
    _raw(relay.port, b"plain")
    recorded = json.dumps(relay.exchanges)

    assert TOKEN not in recorded and "session=x" not in recorded
    assert MARKER_KEY not in recorded and "plain" not in recorded
    assert {e["path"] for e in relay.exchanges} == {"/mcp"}


# --- The wiring: a product double against the real origin ------------------------------------

#: What a Direct server answers once it started a login (``bootstrap``).
_STARTED = (
    "Session expired. A login browser window has been opened. Sign in with "
    "your LinkedIn credentials there, then retry this tool."
)
#: What the owner answers through a frontend whose sign-in did not finish.
_OWNER_ANSWER = (
    "The shared LinkedIn browser's session stopped working, and it cannot sign "
    "in by itself. Retry this tool: the client will open a login window."
)


class _Login:
    """The product's login, as a double: the session retired into
    quarantine, the wall, one ask after another, and once an ask is answered
    a real session on disk and the feed read with it, as the wall's page
    goes on to it. Unanswered within *budget*, it fails and puts the old
    session back, as ``interactive_login`` restores it."""

    def __init__(self, scene: Any, budget: float) -> None:
        self.scene = scene
        self.budget = budget
        self.outcome: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        scene = self.scene
        quarantine = _rotate(scene.directory)
        _exchange(scene.origin, scene.certificates, LOGIN_PATH)
        deadline = time.monotonic() + self.budget
        while time.monotonic() < deadline:
            status, headers, _ = _exchange(
                scene.origin, scene.certificates, LOGIN_POLL_PATH
            )
            if status == 200:
                value = _issued_value(headers)
                _write_session(scene.directory, value)
                _exchange(scene.origin, scene.certificates, "/feed/", value)
                scene.cookie = value
                self.outcome = COMPLETED
                return
            time.sleep(0.02)
        _restore(scene.directory, quarantine)
        self.outcome = FAILED


def _record(name: str, began: int, *, post: bool, text: str = "") -> dict[str, Any]:
    return {
        "tool": name,
        "began": time.time(),
        "began_monotonic_ns": began,
        "ended": time.time(),
        "ended_monotonic_ns": time.monotonic_ns(),
        "outcome": harness.RETURNED,
        "is_error": not post and name != CLOSE_TOOL,
        "read_the_post": post,
        "marked_sections": [],
        "section_errors": [],
        "text": text,
    }


class _RepairScene(_CalibrationScene):
    """The calibration's scene in Direct mode, with a product double behind
    the host: its reads go to the real origin, and a read the origin walls
    starts the double's login and answers that a login started."""

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.directory = profile[0]
        self.login: _Login | None = None
        #: How long the double's login waits for its completion.
        self.budget = 10.0
        #: The double loses the session on its own while closing the browser.
        self.lose_on_close = False
        monkeypatch.setattr(harness, "wait_for_no_browser", self._no_browser)

    def _no_browser(self, account: Any, seconds: float) -> list[int]:
        """The double's login is the only browser there is."""
        login = self.login
        if login is not None:
            login.thread.join(seconds)
            if login.thread.is_alive():
                return [9999]
        return []

    async def _call(self, session, name: str, arguments: dict) -> dict:
        began = time.monotonic_ns()
        if name == CLOSE_TOOL:
            if self.lose_on_close:
                _write_session(self.directory, "synthetic-lost-on-its-own")
            record = _record(name, began, post=False)
        else:
            status = (
                await asyncio.to_thread(
                    _exchange, self.origin, self.certificates, "/feed/", self.cookie
                )
            )[0]
            if status == 200:
                record = _record(name, began, post=True)
            else:
                if self.login is None:
                    self.login = _Login(self, self.budget)
                record = _record(name, began, post=False, text=_STARTED)
                session.user_lines.append(_STARTED)
        session.calls.append(record)
        return record

    async def run_row(self, row: str):
        return await measure_host_quit_row(
            profile=self.tmp_path / "auth" / "profile",
            experiment="K1",
            daemon=False,
            egress=cast(Any, (self.origin, SimpleNamespace(url="x", decisions=[]))),
            log=self.log,
            work_dir=self.tmp_path / "row",
            row=row,
        )


@pytest.fixture
def repair(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _RepairScene(monkeypatch, tmp_path, profile, origin, certificates)


async def test_a_cold_repair_in_direct_replaces_the_session_after_its_rejection(
    repair,
):
    result = await repair.run_row(ROW_COLD)

    assert result.failures == [], result.report()
    vector = result.vector
    assert vector is not None
    # The original generation lost, and told; the replacement beside it.
    assert vector.o4_session == LOST_ANNOUNCED
    assert vector.o4_lineage == LINEAGE_REPLACED
    assert set(vector.o3_authorized) and vector.o3_protected == ()
    record = result.record
    assert record is not None
    [issued] = record["login"]["issued"]
    assert record["lineage"]["replacement"]["digest"] == issued["digest"]
    assert record["authorization"]["kind"] == ORIGIN_REJECTED
    assert record["login"]["closed_ns"] is not None
    # Nothing that could sign in again ran; the origin judged the session.
    repair.preservation.assert_not_awaited()
    assert result.post_quit is not None
    assert result.post_quit.origin_judged and result.post_quit.valid is True
    kinds = [event["kind"] for event in repair.log.records()]
    for kind in ("authorization", "login.rejected", "login.released", "r17.lineage"):
        assert kind in kinds


async def test_a_failed_login_in_direct_is_an_authorized_loss_and_nothing_repairs_it(
    repair,
):
    repair.budget = 0.5
    result = await repair.run_row(ROW_FAILED)

    assert result.failures == [], result.report()
    assert result.vector is not None
    assert result.vector.o4_session == LOST_ANNOUNCED
    assert result.vector.o4_lineage == LINEAGE_LOST
    # The old session is back on disk, and the origin says it is rejected.
    assert result.post_quit is not None and result.post_quit.valid is False
    assert result.record is not None and result.record["login"]["issued"] == []
    repair.preservation.assert_not_awaited()


async def test_a_failed_login_that_never_settles_fails_the_row(repair, monkeypatch):
    repair.budget = 3.0
    monkeypatch.setattr(auth_repair, "FAILED_SETTLE_SECONDS", 0.5)
    try:
        result = await repair.run_row(ROW_FAILED)
    finally:
        repair.origin.close_login()
        if repair.login is not None:
            repair.login.thread.join(10)

    assert any("did not settle within its budget" in f for f in result.failures), (
        result.failures
    )


async def test_an_unexpected_loss_still_fails_after_a_successful_replacement(repair):
    # The session went before the harness rejected it, with the origin still
    # accepting it; the sign-in that followed worked.
    repair.lose_on_close = True
    result = await repair.run_row(ROW_COLD)

    assert result.record is not None
    assert result.record["lineage"]["reading"] == LINEAGE_UNAUTHORIZED
    assert result.record["login"]["issued"]
    assert (
        "O4: the session was lost or replaced without an authorization recorded "
        "before it" in result.failures
    )
    assert any("already lost when the authorization" in f for f in result.failures)


async def test_a_replacement_with_no_recorded_authorization_fails(repair, monkeypatch):
    async def unrecorded(ctx) -> None:
        assert ctx.auth is not None
        # The origin's state changes, but nothing records it as authorized.
        ctx.auth = dataclasses.replace(
            ctx.auth, reject=lambda: {**ctx.origin.reject_sessions(), "snapshot": {}}
        )
        await auth_repair.repair_script(ctx)

    monkeypatch.setitem(
        harness.ROWS,
        ROW_COLD,
        dataclasses.replace(harness.ROWS[ROW_COLD], script=unrecorded),
    )
    result = await repair.run_row(ROW_COLD)

    assert result.vector is not None
    assert result.vector.o4_lineage == LINEAGE_UNAUTHORIZED
    assert (
        "O4: the session was lost or replaced without an authorization recorded "
        "before it" in result.failures
    )


async def test_an_authorization_the_script_writes_itself_authorizes_nothing(
    repair, monkeypatch
):
    async def forged(ctx) -> None:
        assert ctx.auth is not None

        # The origin rejects, and the script claims the authorization in its
        # own record instead of the harness recording it.
        def reject() -> dict[str, Any]:
            found = ctx.origin.reject_sessions()
            ctx.record["authorized"] = ORIGIN_REJECTED
            return {**found, "snapshot": {}}

        ctx.auth = dataclasses.replace(ctx.auth, reject=reject)
        await auth_repair.repair_script(ctx)

    monkeypatch.setitem(
        harness.ROWS,
        ROW_COLD,
        dataclasses.replace(harness.ROWS[ROW_COLD], script=forged),
    )
    result = await repair.run_row(ROW_COLD)

    assert result.vector is not None
    assert result.vector.o4_lineage == LINEAGE_UNAUTHORIZED
    assert any(
        "the row recorded an authorization the harness does not know: "
        "'origin-rejected'" in failure
        for failure in result.failures
    ), result.failures


async def test_a_second_authorization_in_one_row_is_refused(repair, monkeypatch):
    async def twice(ctx) -> None:
        assert ctx.auth is not None
        first = ctx.auth.authorize(LOGIN)
        ctx.record["second_authorization"] = ctx.auth.authorize(LOGIN)
        ctx.record["first_authorization"] = first

    monkeypatch.setitem(
        harness.ROWS,
        ROW_COLD,
        dataclasses.replace(harness.ROWS[ROW_COLD], script=twice),
    )
    result = await repair.run_row(ROW_COLD)

    record = result.record
    assert record is not None
    assert record["second_authorization"]["recorded"] is False
    assert any(
        "the row recorded a second authorization: 'login'" in failure
        for failure in result.failures
    ), result.failures


# --- The scripts in daemon mode, on a modelled owner ----------------------------------------


class _DaemonRepair:
    """What a repair script may touch (``harness.RowContext``) on a modelled
    owner and frontend: the owner's log, the frontend's lines, and reads to
    the real origin, the frontend's sign-in the double above."""

    def __init__(
        self,
        row: str,
        on_disk,
        served: SyntheticOrigin,
        ca: Path,
        *,
        wait: float = 30.0,
        budget: float = 10.0,
        reopen: bool = False,
    ) -> None:
        self.row = row
        self.daemon = True
        self.directory, staged = on_disk
        self.staged = staged
        self.origin, self.certificates = served, ca
        self.cookie = staged.li_at
        served.accept_session(staged.li_at)
        self.wait, self.budget, self.reopen = wait, budget, reopen
        self.login: _Login | None = None
        self.owner_lines: list[str] = []
        self.host_lines: list[str] = []
        self.calls: list[dict[str, Any]] = []
        self.record: dict[str, Any] = {
            "row": row,
            "mode": "daemon",
            "observation_problems": [],
        }
        self.auth = SimpleNamespace(
            reject=self._reject,
            authorize=None,
            release=served.release_login,
            login=served.login_record,
            snapshot=self._snapshot,
            browser_gone=self._browser_gone,
            second_host=self._second_host,
            owner_reading=self._owner_reading,
            owner_log=lambda: list(self.owner_lines),
            host_output=lambda: list(self.host_lines),
        )
        served.arm_login()
        _exchange(served, ca, "/feed/", staged.li_at)
        warm = _record(harness.READ_TOOL, time.monotonic_ns(), post=True)
        self.calls.append(warm)

    def owner(self) -> Any:
        return SimpleNamespace(
            pid=_A[0], create_time=_A[1], instance_id=_A[2], process=None
        )

    def emit(self, *args, **kwargs) -> None:
        pass

    def _snapshot(self, label: str) -> dict[str, Any]:
        seen = snapshot(self.directory, expected_digest=self.staged.li_at_digest)
        return {
            "label": label,
            **seen.as_event_fields(),
            "seen_ns": time.monotonic_ns(),
        }

    def _reject(self) -> dict[str, Any]:
        found = self.origin.reject_sessions()
        seen = self._snapshot("authorization")
        self.record["authorized"] = ORIGIN_REJECTED
        self.record["authorization"] = {
            "kind": ORIGIN_REJECTED,
            "at_ns": found["monotonic_ns"],
            "snapshot": seen,
        }
        return {**found, "snapshot": seen}

    async def _browser_gone(self, seconds: float) -> dict[str, Any]:
        login = self.login
        if login is not None:
            await asyncio.to_thread(login.thread.join, seconds)
        remaining = [9999] if login is not None and login.thread.is_alive() else []
        return {"remaining": remaining, "seen_ns": time.monotonic_ns()}

    async def _owner_reading(self, label: str) -> dict[str, Any]:
        return {**_owner(0), "seen_ns": time.monotonic_ns()}

    def _mark(self) -> None:
        self.owner_lines.append(
            "INFO daemon_auth Asking the client to sign in (stale, replayable=True)"
        )

    async def _feed(self, cookie: str | None) -> int:
        return (
            await asyncio.to_thread(
                _exchange, self.origin, self.certificates, "/feed/", cookie
            )
        )[0]

    async def call(self, name: str, arguments: dict) -> dict:
        """The frontend forwards, the owner meets the wall once and marks it,
        the frontend signs in and waits, and runs the read again once."""
        began = time.monotonic_ns()
        if name == CLOSE_TOOL:
            record = _record(name, began, post=False)
        elif await self._feed(self.cookie) == 200:
            record = _record(name, began, post=True)
        else:
            self._mark()
            if self.login is None:
                self.login = _Login(self, self.budget)
            await asyncio.to_thread(self.login.thread.join, self.wait)
            if self.login.outcome == COMPLETED:
                self.host_lines += [
                    "INFO The sign-in finished",
                    f"INFO {REPLAY_LINE}",
                ]
                post = await self._feed(self.cookie) == 200
                record = _record(name, began, post=post)
            else:
                record = _record(name, began, post=False, text=_OWNER_ANSWER)
        self.calls.append(record)
        return record

    async def _second_host(self) -> dict[str, Any]:
        """A second frontend: its call meets the owner's latch, and once the
        first sign-in finished, it reads with that session."""
        launched = time.monotonic_ns()
        if self.reopen:
            # The owner opening a browser on the stale generation.
            await self._feed(self.staged.li_at)
        self._mark()
        assert self.login is not None
        await asyncio.to_thread(self.login.thread.join, self.wait)
        began = time.monotonic_ns()
        post = await self._feed(self.cookie) == 200
        return {
            "made": True,
            "launched_ns": launched,
            "call": _record(harness.READ_TOOL, began, post=post),
            "forwarded": True,
            "quit_problems": [],
        }

    def finish_record(self) -> dict[str, Any]:
        """What the row entry adds once the script is done."""
        self.origin.close_login()
        if self.login is not None:
            self.login.thread.join(30)
        record = self.record
        record.update(
            platform="linux",
            idle_timeout_seconds=AUTH_IDLE_TIMEOUT_SECONDS,
            k2=dict(K2_NOT_APPLICABLE),
            environment=dict(ENVIRONMENT),
            script_error=None,
            host=_host(),
            egress={"forwarded": ["www.linkedin.com"], "refused": []},
            owner_processes=[_lifetime(*_A[:2])],
            gate_processes=[],
            calls=[harness.call_record(call) for call in self.calls],
            login=self.origin.login_record(),
            requests=[
                {
                    "host": r.host,
                    "path": r.path,
                    "session_valid": r.session_valid,
                    "t": r.t,
                    "monotonic_ns": r.monotonic_ns,
                    "session_digests": list(r.session_digests),
                    "redirected": r.redirected,
                }
                for r in self.origin.requests
            ],
        )
        return json.loads(json.dumps(record))


async def _daemon_row(row: str, *args, **kwargs) -> dict[str, Any]:
    context = _DaemonRepair(row, *args, **kwargs)
    await CASES[row].script(cast(Any, context))
    return context.finish_record()


async def test_the_owner_s_marker_is_repaired_and_replayed_once(
    on_disk,
    served,
    ca,
):
    record = await _daemon_row(ROW_COLD, on_disk, served, ca)

    assert repair_problems(record, daemon=True) == []
    assert record["marked"] == [{"reason": "stale", "replayable": True}]
    assert record["host_lines"]["replayed"] == 1
    assert repair_reading(record)["at_read_end"] == COMPLETED


async def test_a_second_frontend_meets_the_latch_and_ends_on_the_one_fresh_session(
    on_disk,
    served,
    ca,
):
    record = await _daemon_row(ROW_SECOND, on_disk, served, ca)

    assert repair_problems(record, daemon=True) == []
    assert record["marks"] == 2 and len(record["login"]["issued"]) == 1
    assert semantics(record)["second"] is True


async def test_an_owner_reopening_on_the_stale_generation_fails_the_second_frontend(
    on_disk,
    served,
    ca,
):
    record = await _daemon_row(ROW_SECOND, on_disk, served, ca, reopen=True)

    assert any(
        "a browser read on the stale generation during the repair" in problem
        for problem in repair_problems(record, daemon=True)
    )


async def test_a_frontend_whose_wait_runs_out_while_its_login_fails_replays_nothing(
    on_disk,
    served,
    ca,
):
    record = await _daemon_row(ROW_FAILED, on_disk, served, ca, wait=0.2, budget=1.0)

    assert repair_problems(record, daemon=True) == []
    # The frontend's wait ran out first, and the login then failed by itself.
    assert repair_reading(record) == {"at_read_end": WAITING, "settled_failed": True}
    assert record["host_lines"]["replayed"] == 0


# --- The login command on a real terminal, signing in at the real origin ---------------------

#: A stand-in for the frozen CLI's ``--login``: the banner on a terminal, the
#: profile retired, the wall, one ask after another until one is answered,
#: and the session it set saved with a new generation, the feed then read
#: with it. Where the origin is and which CA it trusts is in a file beside it.
_LOGIN_CLI = r"""
import json, os, shutil, socket, ssl, sys, time
from pathlib import Path

here = Path(__file__).parent
where = json.loads((here / "origin.json").read_text())
if sys.stdin.isatty() and sys.stdout.isatty():
    print("%(banner)s0.0.0", flush=True)
assert sys.argv[1:] == ["--login"], sys.argv
profile = Path(os.environ["USER_DATA_DIR"])
root = profile.parent
context = ssl.create_default_context(cafile=where["ca"])


def get(path, cookie=None):
    raw = socket.create_connection(("127.0.0.1", where["port"]), timeout=10)
    with context.wrap_socket(raw, server_hostname="www.linkedin.com") as tls:
        head = [f"GET {path} HTTP/1.1", "Host: www.linkedin.com", "Connection: close"]
        if cookie:
            head.append(f"Cookie: li_at={cookie}")
        tls.sendall(("\r\n".join(head) + "\r\n\r\n").encode())
        data = b""
        while chunk := tls.recv(65536):
            data += chunk
    lines = data.split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
    headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
    return int(lines[0].split(" ")[1]), headers


print("%(login)s", flush=True)
quarantine = root / ("%(prefix)s" + str(time.time_ns()))
quarantine.mkdir()
for name in ("cookies.json", "source-state.json"):
    if (root / name).exists():
        shutil.move(str(root / name), str(quarantine / name))
print("%(opened)s...", flush=True)
get("/login")
while True:
    status, headers = get("%(poll)s")
    if status == 200:
        value = headers["set-cookie"].split(";", 1)[0].removeprefix("li_at=")
        break
    time.sleep(0.05)
cookie = {
    "name": "li_at", "value": value, "domain": ".linkedin.com", "path": "/",
    "expires": time.time() + 30 * 24 * 3600, "httpOnly": True, "secure": True,
    "sameSite": "None",
}
(root / "cookies.json").write_text(json.dumps([cookie]))
from linkedin_mcp_server.session_state import write_source_state

write_source_state(profile)
get("/feed/", value)
print("%(saved)s " + str(profile), flush=True)
""" % {
    "banner": "🔗 LinkedIn MCP Server v",
    "login": "LinkedIn MCP Server - Profile Creation",
    "prefix": QUARANTINE_PREFIX,
    "opened": LOGIN_OPENED,
    "poll": LOGIN_POLL_PATH,
    "saved": PROFILE_SAVED,
}


class _QuitOnly:
    def __init__(self) -> None:
        self.quit_done = False

    async def host_quit(self) -> None:
        self.quit_done = True


class _LoginScene(_CalibrationScene):
    """The calibration's scene with a host the script can quit and the
    login stand-in as the row's command line."""

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.cli = self.tmp_path / "cli" / "fake_cli.py"
        self.cli.parent.mkdir()
        self.cli.write_text(_LOGIN_CLI)
        (self.cli.parent / "origin.json").write_text(
            json.dumps(
                {
                    "port": origin.port,
                    "ca": str(certificates / synthetic_origin.CA_FILE),
                }
            )
        )
        (self.tmp_path / "auth" / "profile.lock").touch()

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
                return await self._call(session, name, arguments)

            try:
                await row_script(call, _QuitOnly())
            except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                session.script_error = f"{type(exc).__name__}: {exc}"
        session.eof_monotonic_ns = time.monotonic_ns()
        session.exit_seen_monotonic_ns = time.monotonic_ns()
        return session


@posix
async def test_a_confirmed_login_in_direct_replaces_the_session_it_was_authorized_for(
    monkeypatch,
    tmp_path,
    on_disk,
    served,
    ca,
):
    scene = _LoginScene(monkeypatch, tmp_path, on_disk, served, ca)
    result = await measure_host_quit_row(
        profile=tmp_path / "auth" / "profile",
        experiment="K1",
        daemon=False,
        egress=cast(Any, (served, SimpleNamespace(url="x", decisions=[]))),
        log=scene.log,
        work_dir=tmp_path / "row",
        row=ROW_LOGIN,
        command=[sys.executable, str(scene.cli)],
    )

    assert result.failures == [], result.report()
    assert result.vector is not None
    # Nobody was told the session went: the user replaced it themselves.
    assert result.vector.o4_session == LOST_SILENT
    assert result.vector.o4_lineage == LINEAGE_REPLACED
    assert result.record is not None and result.record["authorized"] == LOGIN
    assert result.record["login_command"]["returncode"] == 0
    scene.preservation.assert_not_awaited()
