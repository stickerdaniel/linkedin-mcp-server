"""The sign-in view relay: framing, endpoints, and two servers on one auth root.

The endpoint cases use the real Unix socket under a short temporary auth root
(``sockaddr_un`` holds about 104 bytes, more than pytest's own temporary paths
allow on macOS). The server cases build two production Direct servers in one
process on that root: the second one knows no open, so it reaches the first
one's login through the socket exactly as a second server process would. The
real two-process case is in ``test_login_relay_processes.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import socket
import stat
import struct
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp import Client
from test_login_app import (
    StandInLogin,
    _attach,
    _frame,
    _images,
    _open_shown,
    _reply,
    _text,
    _ui_client,
    _until,
)
from test_server import _stand_in_the_lifespan

import linkedin_mcp_server.bootstrap as bootstrap
import linkedin_mcp_server.login_app as login_app
from linkedin_mcp_server import login_relay, session_state
from linkedin_mcp_server.config import reset_config
from linkedin_mcp_server.exceptions import ProfileRootRefusedError
from linkedin_mcp_server.login_relay import Envelope, RelayProtocolError
from linkedin_mcp_server.login_view import MAX_IMAGE_BASE64, LoginAttempt
from linkedin_mcp_server.profile_claim import ensure_profile_claim
from linkedin_mcp_server.server import create_mcp_server

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
    reason="the relay runs on Unix sockets",
)

_VIEW = "linkedin.dev/login-view"


@pytest.fixture
def short_root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A claimed source profile under a temporary directory short enough for a socket."""
    base = Path(tempfile.mkdtemp(prefix="lr-", dir="/tmp"))
    profile = session_state.canonical(base / "auth" / "profile")
    monkeypatch.setenv("USER_DATA_DIR", str(profile))
    reset_config()
    monkeypatch.setattr(session_state, "get_source_profile_dir", lambda: profile)
    ensure_profile_claim(profile)
    try:
        yield profile
    finally:
        login_relay.reset_for_testing()
        shutil.rmtree(base, ignore_errors=True)


def _reader(data: bytes, *, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    if eof:
        reader.feed_eof()
    return reader


class TestFraming:
    async def test_a_message_round_trips(self):
        content = {"tool": "login_view_frame", "arguments": {"after": "ä"}}

        assert await login_relay.read_message(_reader(login_relay.encode(content))) == (
            content
        )

    @pytest.mark.parametrize(
        "body",
        [b"\xff\xfe", b'{"a": 1, "a": 2}', b'{"a": NaN}', b"[1, 2]"],
        ids=["not utf-8", "repeated key", "non-JSON constant", "not an object"],
    )
    async def test_what_encode_cannot_produce_is_refused(self, body: bytes):
        frame = struct.pack("!I", len(body)) + body

        with pytest.raises(RelayProtocolError):
            await login_relay.read_message(_reader(frame))

    async def test_an_oversized_length_is_refused_before_any_body_arrives(self):
        # No body and no end of stream: a reader that waited for it would hang.
        header = struct.pack("!I", login_relay.MAX_MESSAGE_BYTES + 1)

        with pytest.raises(RelayProtocolError):
            await asyncio.wait_for(
                login_relay.read_message(_reader(header, eof=False)), 1
            )

    async def test_a_cut_message_is_an_error_and_a_clean_close_is_not(self):
        with pytest.raises(RelayProtocolError):
            await login_relay.read_message(_reader(struct.pack("!I", 10) + b"{}"))
        with pytest.raises(EOFError):
            await login_relay.read_message(_reader(b""))

    def test_encode_refuses_more_than_the_cap(self):
        with pytest.raises(RelayProtocolError):
            login_relay.encode({"text": "x" * login_relay.MAX_MESSAGE_BYTES})

    def test_a_reply_of_the_wrong_shape_is_refused(self):
        with pytest.raises(RelayProtocolError):
            Envelope.from_wire({"text": "ok", "image": None, "meta": None})


async def _echo(operation: str, arguments: Mapping[str, object]) -> Envelope:
    return Envelope(False, json.dumps({"op": operation, **arguments}))


def _attempt_with(task_body: Any) -> tuple[LoginAttempt, asyncio.Task[None]]:
    attempt = LoginAttempt()
    return attempt, attempt.start(task_body(), name="login")


async def _forever() -> None:
    await asyncio.Event().wait()


async def _ask(attempt_id: str, **arguments: object) -> login_relay.Forwarded:
    return await login_relay.forward(
        "login_view_frame", arguments, attempt_id, deadline_s=3
    )


class TestEndpoints:
    @pytest.fixture(autouse=True)
    def echo(self, short_root: Path) -> None:
        login_relay.install(_echo, status_tail_s=lambda: 0.5)

    async def test_it_listens_privately_before_serve_returns(self, short_root: Path):
        attempt, task = _attempt_with(_forever)

        login_relay.serve(attempt, task)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        mode = stat.S_IMODE(os.lstat(path).st_mode)
        directory = stat.S_IMODE(os.lstat(path.parent).st_mode)
        answered = await _ask(attempt.id, after="x")

        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        assert (mode, directory) == (0o600, 0o700)
        assert answered.delivery == "answered" and answered.envelope is not None
        assert json.loads(answered.envelope.text) == {
            "op": "login_view_frame",
            "after": "x",
        }
        task.cancel()

    async def test_two_attempts_get_two_endpoints(self, short_root: Path):
        first, first_task = _attempt_with(_forever)
        second, second_task = _attempt_with(_forever)

        login_relay.serve(first, first_task)
        login_relay.serve(second, second_task)

        assert login_relay.endpoint_path(first.id) != login_relay.endpoint_path(
            second.id
        )
        for attempt in (first, second):
            assert (await _ask(attempt.id)).delivery == "answered"
        first_task.cancel()
        second_task.cancel()

    async def test_an_ended_attempt_keeps_its_endpoint_for_the_tail_only(
        self, short_root: Path
    ):
        ended, ended_task = _attempt_with(_forever)
        login_relay.serve(ended, ended_task)
        ended_task.cancel()
        await asyncio.gather(ended_task, return_exceptions=True)
        running, running_task = _attempt_with(_forever)
        login_relay.serve(running, running_task)

        within = await _ask(ended.id)
        await asyncio.sleep(0.8)
        after = await _ask(ended.id)

        assert within.delivery == "answered"
        assert after.delivery == "unavailable"
        assert (await _ask(running.id)).delivery == "answered"
        path = login_relay.endpoint_path(ended.id)
        assert path is not None and not path.exists()
        running_task.cancel()

    async def test_close_leaves_a_socket_it_did_not_make(self, short_root: Path):
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        # Another listener now holds the name.
        path.unlink()
        foreign = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        foreign.bind(str(path))

        await login_relay.close_all()

        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        foreign.close()
        task.cancel()

    @pytest.mark.parametrize("kind", ["file", "symlink"])
    async def test_something_already_at_the_path_is_kept_and_no_relay_starts(
        self, short_root: Path, kind: str
    ):
        attempt, task = _attempt_with(_forever)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = path.parent / "elsewhere"
        target.write_text("kept")
        if kind == "file":
            path.write_text("kept")
        else:
            path.symlink_to(target)

        login_relay.serve(attempt, task)

        assert (await _ask(attempt.id)).delivery == "unavailable"
        assert path.read_text() == "kept" and target.read_text() == "kept"
        task.cancel()

    async def test_nothing_is_removed_without_the_claim(
        self, short_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        assert (await _ask(attempt.id)).delivery == "answered"

        def refused(_profile: object) -> Path:
            raise ProfileRootRefusedError("not claimed")

        monkeypatch.setattr(session_state, "_owned", refused)
        await login_relay.close_all()

        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        task.cancel()

    async def test_a_socket_that_refuses_is_reported_and_left_alone(
        self, short_root: Path
    ):
        attempt = LoginAttempt()
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # A listener that died: the name stays, nobody accepts.
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(path))
        dead.close()

        assert (await _ask(attempt.id)).delivery == "unavailable"
        assert stat.S_ISSOCK(os.lstat(path).st_mode)

    async def test_stop_ends_work_already_accepted(self, short_root: Path):
        entered = asyncio.Event()

        async def blocks(_operation: str, _arguments: Mapping[str, object]) -> Envelope:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        login_relay.reset_for_testing()
        login_relay.install(blocks, status_tail_s=lambda: 0.5)
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)
        asking = asyncio.create_task(_ask(attempt.id))
        await asyncio.wait_for(entered.wait(), 3)

        await asyncio.wait_for(login_relay.close_all(), 3)

        assert (await asking).delivery == "lost"
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None and not path.exists()
        task.cancel()

    async def test_stop_login_ends_a_reply_its_reader_stopped_reading(
        self, short_root: Path
    ):
        answered = asyncio.Event()

        async def largest_frame(
            _operation: str, _arguments: Mapping[str, object]
        ) -> Envelope:
            answered.set()
            return Envelope(False, "{}", image="A" * MAX_IMAGE_BASE64)

        login_relay.reset_for_testing()
        login_relay.install(largest_frame, status_tail_s=lambda: 30)
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        # Connected, and never reads the reply.
        _reader, writer = await asyncio.open_unix_connection(str(path))
        await login_relay.write_message(
            writer, {"tool": "login_view_frame", "arguments": {}}
        )
        await asyncio.wait_for(answered.wait(), 3)
        await asyncio.sleep(0.2)

        await asyncio.wait_for(bootstrap.stop_login(), 3)

        assert not path.exists()
        writer.close()
        task.cancel()

    async def test_a_send_the_owner_never_reads_ends_at_the_deadline(
        self, short_root: Path
    ):
        attempt = LoginAttempt()
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        async def never_reads(
            _reader: asyncio.StreamReader, _writer: asyncio.StreamWriter
        ) -> None:
            await asyncio.Event().wait()

        stuck = await asyncio.start_unix_server(never_reads, path=str(path))
        try:
            sent = await asyncio.wait_for(
                login_relay.forward(
                    "login_view_input",
                    {"events": "x" * 900_000},
                    attempt.id,
                    deadline_s=0.5,
                ),
                2,
            )
        finally:
            stuck.close()

        assert sent.delivery == "lost"

    async def test_an_answer_is_returned_when_closing_its_stream_stalls(
        self, short_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)

        async def held(_writer: asyncio.StreamWriter) -> None:
            await asyncio.Event().wait()

        monkeypatch.setattr(asyncio.StreamWriter, "wait_closed", held)
        sent = await asyncio.wait_for(
            login_relay.forward(
                "login_view_frame", {"after": "x"}, attempt.id, deadline_s=1
            ),
            2,
        )

        assert sent.delivery == "answered" and sent.envelope is not None
        assert json.loads(sent.envelope.text)["after"] == "x"
        task.cancel()

    async def test_a_request_for_another_tool_gets_no_answer(self, short_root: Path):
        attempt, task = _attempt_with(_forever)
        login_relay.serve(attempt, task)
        path = login_relay.endpoint_path(attempt.id)
        assert path is not None

        reader, writer = await asyncio.open_unix_connection(str(path))
        await login_relay.write_message(
            writer, {"tool": "close_session", "arguments": {}}
        )
        with pytest.raises(EOFError):
            await asyncio.wait_for(login_relay.read_message(reader), 3)
        writer.close()
        task.cancel()

    @pytest.mark.parametrize("cause", ["path too long", "no Unix sockets"])
    async def test_without_a_usable_socket_there_is_no_relay(
        self, short_root: Path, monkeypatch: pytest.MonkeyPatch, cause: str
    ):
        if cause == "path too long":
            monkeypatch.setattr(login_relay, "_PATH_LIMIT", 20)
        else:
            monkeypatch.setattr(login_relay, "_SUPPORTED", False)
        attempt, task = _attempt_with(_forever)

        login_relay.serve(attempt, task)

        assert login_relay.endpoint_path(attempt.id) is None
        assert (await _ask(attempt.id)).delivery == "unavailable"
        assert not (short_root.parent / login_relay.ENDPOINT_DIR).exists()
        task.cancel()


# ---------------------------------------------------------------------------
# Two production servers on one auth root
# ---------------------------------------------------------------------------


@pytest.fixture
def login(short_root: Path, monkeypatch: pytest.MonkeyPatch) -> StandInLogin:
    """A Docker server's login on the short root, as in test_login_app."""
    stand_in = StandInLogin(short_root)
    _stand_in_the_lifespan(monkeypatch)
    bootstrap.initialize_bootstrap("docker")
    monkeypatch.setattr(bootstrap, "interactive_login", stand_in)
    monkeypatch.setattr(bootstrap, "_ensure_browser_installed", AsyncMock())
    monkeypatch.setattr(login_app, "require_persistent_profile_mount", lambda _p: None)
    return stand_in


@pytest.fixture
async def owner(login: StandInLogin) -> AsyncIterator[Client]:
    """The model's server process: it runs open_login and owns the login."""
    async with _ui_client(create_mcp_server()) as connected:
        yield connected
    login.finish.set()


@pytest.fixture
async def other(owner: Client) -> AsyncIterator[Client]:
    """The app's server process: it knows no open and relays every view call."""
    async with Client(create_mcp_server()) as connected:
        yield connected


async def _relayed_view(owner: Client, other: Client) -> dict[str, str]:
    shown = await _open_shown(owner)
    result, view = await _attach(other, shown)
    assert view is not None, _text(result)
    return view


def _input(view: dict[str, str], frame: dict[str, Any], *events: dict[str, object]):
    return {
        **view,
        "page_id": frame["page_id"],
        "geometry": frame["geometry"],
        "events": list(events),
    }


class TestTwoServers:
    async def test_the_other_server_shows_and_drives_the_owners_login(
        self, owner: Client, other: Client, login: StandInLogin
    ):
        shown = await _open_shown(owner)
        attached, view = await _attach(other, shown)
        assert view is not None
        frame = await _until(other, view, "image")
        reply = _reply(frame)

        typed = await other.call_tool_mcp(
            "login_view_input",
            _input(
                view,
                reply,
                {"type": "type", "text": "dä.test@example.com"},
                {"type": "paste", "text": "pässwörd ✓ 🔑"},
            ),
        )

        assert view["grant"] not in _text(attached)
        assert (attached.meta or {})[_VIEW]["attempt_id"] == shown["attempt_id"]
        assert reply["page_id"] == "p1" and len(_images(frame)) == 1
        assert _reply(typed) == {"access": "full", "input": "applied", "applied": 2}
        assert login.page.received == [
            ("type", "dä.test@example.com"),
            ("insert_text", "pässwörd ✓ 🔑"),
        ]

    async def test_after_settlement_only_status_crosses_until_the_tail_ends(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(login_app, "STATUS_TAIL_S", 1.5)
        shown = await _open_shown(owner)
        view = await _relayed_view_from(other, shown)
        frame = _reply(await _until(other, view, "image"))
        login.finish.set()

        ended = await _until(other, view, "signed_in")
        typed = await other.call_tool_mcp(
            "login_view_input", _input(view, frame, {"type": "type", "text": "x"})
        )
        _, again = await _attach(other, shown)
        await asyncio.sleep(1.7)
        gone = await _frame(other, view)

        assert _reply(ended)["access"] == "status" and _images(ended) == []
        assert _reply(typed)["input"] == "refused"
        assert again == view
        assert _reply(gone) == {"access": "unavailable"}
        assert ("type", "x") not in login.page.received

    async def test_a_batch_cut_short_at_the_owner_has_an_unknown_outcome(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        view = await _relayed_view(owner, other)
        frame = _reply(await _until(other, view, "image"))
        monkeypatch.setitem(login_app._OWNER_DEADLINE_S, "login_view_input", 0.5)
        keyboard_type = login.page.keyboard.type

        async def second_one_hangs(text: str) -> None:
            await keyboard_type(text)
            if len(login.page.received) > 1:
                await asyncio.Event().wait()

        setattr(login.page.keyboard, "type", second_one_hangs)

        result = await other.call_tool_mcp(
            "login_view_input",
            _input(
                view,
                frame,
                {"type": "type", "text": "a"},
                {"type": "type", "text": "b"},
                {"type": "type", "text": "c"},
            ),
        )

        assert result.is_error
        assert (result.meta or {})["linkedin.dev/error"]["code"] == "outcome_unknown"
        assert _text(result) == login_app.INPUT_OUTCOME_UNKNOWN
        assert login.page.received == [("type", "a"), ("type", "b")]

    async def test_an_answer_lost_after_the_batch_landed_has_an_unknown_outcome(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        view = await _relayed_view(owner, other)
        frame = _reply(await _until(other, view, "image"))

        async def reply_lost(writer: asyncio.StreamWriter, _envelope: Envelope) -> None:
            writer.transport.abort()

        monkeypatch.setattr(login_relay, "_write_reply", reply_lost)
        result = await other.call_tool_mcp(
            "login_view_input",
            _input(view, frame, {"type": "paste", "text": "landed"}),
        )

        assert result.is_error
        assert (result.meta or {})["linkedin.dev/error"]["code"] == "outcome_unknown"
        assert login.page.received == [("insert_text", "landed")]

    async def test_with_no_owner_listening_nothing_is_sent(
        self, owner: Client, other: Client, login: StandInLogin
    ):
        view = await _relayed_view(owner, other)
        frame = _reply(await _until(other, view, "image"))

        await login_relay.close_all()
        typed = await other.call_tool_mcp(
            "login_view_input", _input(view, frame, {"type": "type", "text": "x"})
        )
        read = await _frame(other, view)

        assert _reply(typed) == {"access": "unavailable", "input": "refused"}
        assert _reply(read) == {"access": "unavailable"}
        assert login.page.received == []

    async def test_stop_login_ends_the_route(
        self, owner: Client, other: Client, login: StandInLogin
    ):
        shown = await _open_shown(owner)

        await bootstrap.stop_login()
        result, view = await _attach(other, shown)

        assert view is None and _reply(result) == {"access": "unavailable"}

    async def test_another_attempt_named_at_an_endpoint_gets_one_local_denial(
        self, owner: Client, login: StandInLogin
    ):
        shown = await _open_shown(owner)

        frame = await login_relay.forward(
            "login_view_frame",
            {"grant": "g", "attempt_id": "AAAAAAAAAAAA"},
            shown["attempt_id"],
            deadline_s=3,
        )
        attach = await login_relay.forward(
            "login_view_attach",
            {"attempt_id": "AAAAAAAAAAAA", "open_id": shown["open_id"]},
            shown["attempt_id"],
            deadline_s=3,
        )

        assert frame.envelope is not None and json.loads(frame.envelope.text) == {
            "access": "denied"
        }
        assert attach.envelope is not None and json.loads(attach.envelope.text) == {
            "access": "none"
        }
        assert len(login.calls) == 1

    async def test_relayed_arguments_are_validated_without_their_values(
        self, owner: Client, other: Client, login: StandInLogin
    ):
        view = await _relayed_view(owner, other)
        captured = io.StringIO()
        handler = logging.StreamHandler(captured)
        root = logging.getLogger()
        previous = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            sent = await login_relay.forward(
                "login_view_input",
                {
                    **view,
                    "page_id": "p1",
                    "geometry": "800x600",
                    "events": [
                        {"type": "paste", "text": "SYNTHETIC-SECRET" * 300},
                        {"type": "type", "text": "x", "extra": "SYNTHETIC-EXTRA"},
                    ],
                },
                view["attempt_id"],
                deadline_s=3,
            )
        finally:
            root.removeHandler(handler)
            root.setLevel(previous)

        assert sent.envelope is not None and sent.envelope.is_error
        assert (sent.envelope.meta or {})["linkedin.dev/error"][
            "code"
        ] == "invalid_argument"
        assert "string_too_long" in sent.envelope.text
        for secret in ("SYNTHETIC-SECRET", "SYNTHETIC-EXTRA", view["grant"]):
            assert secret not in sent.envelope.text
            assert secret not in captured.getvalue()
        assert login.page.received == []

    async def test_an_owner_error_keeps_its_flag_and_marker_in_the_other_server(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        view = await _relayed_view(owner, other)
        monkeypatch.setitem(login_app._OWNER_DEADLINE_S, "login_view_frame", 0.2)
        login.page.gate = asyncio.Event()

        result = await _frame(other, view, budget=3)

        assert result.is_error
        assert (result.meta or {})["linkedin.dev/error"]["code"] == "internal"
        assert _images(result) == []
        login.page.gate.set()

    async def test_without_unix_sockets_the_owner_still_shows_its_own_view(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        monkeypatch.setattr(login_relay, "_SUPPORTED", False)
        shown = await _open_shown(owner)

        _, here = await _attach(owner, shown)
        elsewhere, nothing = await _attach(other, shown)

        assert here is not None
        assert len(_images(await _until(owner, here, "image"))) == 1
        assert nothing is None and _reply(elsewhere) == {"access": "unavailable"}

    async def test_a_relay_that_fails_to_start_is_unavailable_at_once(
        self,
        owner: Client,
        other: Client,
        login: StandInLogin,
        monkeypatch: pytest.MonkeyPatch,
    ):
        async def fails(*_args: object, **_kwargs: object) -> asyncio.Server:
            raise OSError("no server")

        monkeypatch.setattr(asyncio, "start_unix_server", fails)
        shown = await _open_shown(owner)

        elsewhere, nothing = await asyncio.wait_for(_attach(other, shown), 3)
        _, here = await _attach(owner, shown)

        assert nothing is None and _reply(elsewhere) == {"access": "unavailable"}
        path = login_relay.endpoint_path(shown["attempt_id"])
        assert path is not None and not path.exists()
        assert here is not None
        assert len(_images(await _until(owner, here, "image"))) == 1


async def _relayed_view_from(other: Client, shown: dict[str, str]) -> dict[str, str]:
    result, view = await _attach(other, shown)
    assert view is not None, _text(result)
    return view
