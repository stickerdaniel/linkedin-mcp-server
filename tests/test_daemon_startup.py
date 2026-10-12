"""The startup protocol between a frontend and the owner it spawns.

Most of what matters here is about two processes and the pipes between them:
which bytes cross, which never do, and that a child the frontend cannot trust is
dead before the frontend moves on. So the refusal cases run a real child through
the real ``_spawn``, and only the codec and the stream ownership are checked
inside one interpreter.
"""

from __future__ import annotations

import io
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from test_daemon_config import _Frontend, _greeting

import linkedin_mcp_server
import linkedin_mcp_server.daemon.descriptor as daemon_descriptor
import linkedin_mcp_server.daemon.election as election_module
import zipfile
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import config as daemon_config
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.daemon import version as daemon_version
from linkedin_mcp_server.daemon.core.spawn import Started
from linkedin_mcp_server.daemon.discovery import _DescriptorInspector
from linkedin_mcp_server.daemon.election import obtain_owner

_NONCE = "0123456789abcdef" * 4
_OTHER_NONCE = "fedcba9876543210" * 4

#: A bound on every wait below, so a regression fails an assertion instead of
#: hanging the suite.
_PARKED = 5.0

#: How long a thread that has nothing left to do may take to end.
_BOUNDED = 1.0

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="drives POSIX signals or an ungated spawn",
)


def _frame(kind: str, nonce: str = _NONCE, **fields: object) -> bytes:
    return startup.encode(startup.message(kind, nonce, **fields))


def _ack(nonce: str = _NONCE) -> bytes:
    return _frame(
        startup.ACK,
        nonce,
        protocol=startup.PROTOCOL,
        build=daemon_version.build_identity(),
    )


def _config(profile: Path) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    config.server.daemon_enabled = True
    return config


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


class TestFraming:
    """A frame is a 4-byte big-endian length and 1 to 65,536 bytes of JSON."""

    def test_a_frame_reads_back_as_what_was_written(self):
        content = startup.message(startup.PREPARED, _NONCE, instance="generation")

        assert startup.read_frame(io.BytesIO(startup.encode(content)).read) == content

    @pytest.mark.parametrize(
        ("wire", "complaint"),
        [
            (struct.pack("!I", 0), "announces 0 bytes"),
            (
                struct.pack("!I", startup.MAX_FRAME_BYTES + 1),
                f"announces {startup.MAX_FRAME_BYTES + 1} bytes",
            ),
            (b"\x00\x00", "header was cut off"),
            (struct.pack("!I", 10) + b'{"a":', "body was cut off"),
            (struct.pack("!I", 3) + b"\xff\xfe!", "not valid JSON"),
            (struct.pack("!I", 3) + b"[1]", "not an object"),
            (struct.pack("!I", 3) + b"NaN", "non-JSON constant"),
            (struct.pack("!I", 13) + b'{"a":1,"a":2}', "repeats a key"),
            # Deeper than the interpreter's recursion limit, inside one frame.
            (
                struct.pack("!I", 60_000) + b"[" * 30_000 + b"]" * 30_000,
                "not valid JSON",
            ),
        ],
        ids=[
            "zero-length",
            "oversize",
            "partial-header",
            "partial-body",
            "invalid-utf8",
            "not-an-object",
            "nan",
            "duplicate-key",
            "nesting",
        ],
    )
    def test_anything_else_is_refused(self, wire: bytes, complaint: str):
        with pytest.raises(startup.ProtocolError, match=complaint):
            startup.read_frame(io.BytesIO(wire).read)

    def test_an_oversize_length_is_refused_before_its_body_is_read(self):
        requested: list[int] = []
        wire = io.BytesIO(struct.pack("!I", 0xFFFFFFFF) + b"x" * 64)

        def read(size: int) -> bytes:
            requested.append(size)
            return wire.read(size)

        with pytest.raises(startup.ProtocolError):
            startup.read_frame(read)

        assert requested == [4]

    def test_end_of_file_between_frames_is_not_a_frame(self):
        with pytest.raises(EOFError):
            startup.read_frame(io.BytesIO(b"").read)

    def test_a_frame_too_large_to_send_is_never_written(self):
        oversize = {"browser": {"proxy_bypass": "x" * startup.MAX_FRAME_BYTES}}

        assert not startup.fits(startup.CONFIG, config=oversize)
        with pytest.raises(startup.ProtocolError):
            startup.encode(startup.message(startup.CONFIG, _NONCE, config=oversize))

    @pytest.mark.parametrize(
        "content",
        [
            {"type": startup.ACK, "nonce": _NONCE, "protocol": 5},
            startup.message(startup.ACK, _NONCE, protocol=True, build="b"),
            startup.message(startup.ACK, _NONCE, protocol=5.0, build="b"),
            startup.message(startup.ACK, _OTHER_NONCE, protocol=5, build="b"),
            startup.message(startup.ACK, _NONCE, protocol=5, build="b", extra=1),
            startup.message(startup.COMMIT, _NONCE, instance="two words"),
            startup.message(startup.VERDICT, _NONCE, status="ready"),
            startup.message(startup.VERDICT, _NONCE, status=["committed"]),
            startup.message(startup.CONFIG, _NONCE, config=[1]),
        ],
        ids=[
            "missing-field",
            "bool-protocol",
            "float-protocol",
            "other-nonce",
            "extra-field",
            "spaced-instance",
            "unknown-verdict",
            "unhashable-verdict",
            "config-not-object",
        ],
    )
    def test_a_frame_is_accepted_only_exactly_as_its_kind_is_defined(
        self, content: dict[str, object]
    ):
        kind = cast(str, content["type"])

        with pytest.raises(startup.ProtocolError):
            startup.expect(content, kind, nonce=_NONCE)

    def test_hello_is_where_the_owner_learns_the_nonce(self):
        hello = startup.message(startup.HELLO, _NONCE, protocol=startup.PROTOCOL)

        assert startup.expect(hello, startup.HELLO, nonce=None) == hello
        with pytest.raises(startup.ProtocolError, match="no valid nonce"):
            startup.expect(
                startup.message(startup.HELLO, "short", protocol=startup.PROTOCOL),
                startup.HELLO,
                nonce=None,
            )


class _Child:
    """The two pipes ``Popen`` hands the frontend, as in-memory streams."""

    def __init__(self, stdout: Any, stdin: Any = None) -> None:
        self.stdin = io.BytesIO() if stdin is None else stdin
        self.stdout = stdout


class TestTheFrontendsEnd:
    def test_startup_noise_before_the_sync_line_is_skipped(self):
        # What a `.pth` file or `sitecustomize` can print before the owner
        # claims its output, a forged sync line for another nonce included.
        noise = (
            b"sitecustomize: loaded\n"
            + startup.sync_line(_OTHER_NONCE)
            + b"no newline at the end"
        )
        child = _Child(io.BytesIO(noise + startup.sync_line(_NONCE) + _ack()))
        channel = startup.FrontendChannel(child, _NONCE)
        try:
            answer = channel.receive(timeout=_PARKED)
        finally:
            channel.close()

        assert answer is not None
        assert startup.expect(answer, startup.ACK, nonce=_NONCE)["build"] == (
            daemon_version.build_identity()
        )

    @pytest.mark.parametrize(
        "output",
        [
            # A frame for another starter after a genuine sync line.
            startup.sync_line(_NONCE) + _ack(_OTHER_NONCE),
            # Frames with no sync line in front of them.
            _ack(),
            # Text after the sync line, where only frames may follow.
            startup.sync_line(_NONCE) + b"Traceback (most recent call last):\n",
        ],
        ids=["other-nonce", "no-sync-line", "text-after-sync"],
    )
    def test_output_the_protocol_does_not_permit_ends_the_exchange(self, output: bytes):
        channel = startup.FrontendChannel(_Child(io.BytesIO(output)), _NONCE)
        try:
            assert channel.receive(timeout=_PARKED) is None
            # And it stays ended: nothing after the refusal is ever read.
            assert channel.receive(timeout=0) is None
        finally:
            channel.close()

    def test_a_verdict_is_the_last_frame_read(self):
        # A committed owner keeps its output open for as long as it serves, so
        # a reader that went on after the VERDICT would sit in that pipe for
        # the owner's whole life, and on Windows keep it from being closed.
        release = threading.Event()
        read_on = threading.Event()

        class _OwnerStillRunning(io.BytesIO):
            def read(self, size: int | None = -1) -> bytes:
                data = super().read(size)
                if not data:
                    read_on.set()
                    release.wait(_PARKED)
                return data

        output = (
            startup.sync_line(_NONCE)
            + _ack()
            + _frame(startup.VERDICT, status=startup.COMMITTED)
        )
        channel = startup.FrontendChannel(_Child(_OwnerStillRunning(output)), _NONCE)
        try:
            assert channel.receive(timeout=_PARKED) is not None
            verdict = channel.receive(timeout=_PARKED)
            # A reader that goes on does so at once; a second is a wide margin.
            listened_on = read_on.wait(_BOUNDED)
            after = channel.receive(timeout=0)
        finally:
            release.set()
            channel.close()

        assert verdict is not None and verdict["status"] == startup.COMMITTED
        assert not listened_on, "the reader kept listening after the verdict"
        assert after is None

    def test_the_channel_takes_both_pipes_out_of_popen(self):
        child = _Child(io.BytesIO(b""))
        channel = startup.FrontendChannel(child, _NONCE)
        try:
            assert (child.stdin, child.stdout) == (None, None)
        finally:
            channel.close()

    def test_a_write_the_child_never_takes_is_bounded_by_the_caller(self):
        release = threading.Event()

        class _Full(io.BytesIO):
            def write(self, data: Any) -> int:
                release.wait(_PARKED)
                raise BrokenPipeError

        channel = startup.FrontendChannel(
            _Child(io.BytesIO(b""), stdin=_Full()), _NONCE
        )
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError):
                channel.send(startup.HELLO, protocol=startup.PROTOCOL, timeout=0.05)
            elapsed = time.monotonic() - started
            # Nothing more is queued behind a write that may still complete.
            with pytest.raises(OSError):
                channel.send(startup.CONFIG, config={}, timeout=0.05)
        finally:
            release.set()
            channel.close()

        assert elapsed < 1.0


class _Pipe:
    """A child's output that parks its read until the child writes.

    ``blocking_close`` is the Windows behaviour: a close waits for a read in
    flight instead of ending it, which is what POSIX does.
    """

    def __init__(self, payload: bytes = b"", *, blocking_close: bool = False) -> None:
        self._payload = io.BytesIO(payload)
        self._parked = not payload
        self._blocking_close = blocking_close
        self.reading = threading.Event()
        self.wrote = threading.Event()
        self.shut = threading.Event()
        self.closes = 0
        self.detaches = 0

    def _wait(self) -> None:
        self.reading.set()
        if self._parked:
            self.wrote.wait(_PARKED)

    def readline(self, size: int = -1) -> bytes:
        self._wait()
        return self._payload.readline(size)

    def read(self, size: int = -1) -> bytes:
        self._wait()
        return self._payload.read(size)

    def detach(self) -> _Pipe:
        self.detaches += 1
        return self

    def close(self) -> None:
        if self._blocking_close and self._parked:
            self.wrote.wait(_PARKED)
        self.closes += 1
        self.wrote.set()
        self.shut.set()


class TestClosingTheOutput:
    def test_windows_leaves_a_parked_reader_holding_the_pipe(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A committed owner holds its output open, so closing here would wait."""
        monkeypatch.setattr(startup, "_IS_WINDOWS", True)
        pipe = _Pipe(blocking_close=True)
        channel = startup.FrontendChannel(_Child(pipe), _NONCE)

        with pytest.raises(TimeoutError):
            channel.receive(timeout=0.05)
        assert pipe.reading.wait(_PARKED)

        started = time.monotonic()
        channel.close()
        elapsed = time.monotonic() - started

        assert elapsed < 1.0
        assert (pipe.detaches, pipe.closes) == (0, 0)

        # The child stops writing, and the reader closes what it kept, once.
        pipe.wrote.set()
        assert pipe.shut.wait(_PARKED)
        assert pipe.closes == 1

    @pytest.mark.parametrize(
        ("windows", "make_pipe"),
        [
            (
                True,
                lambda: _Pipe(
                    startup.sync_line(_NONCE)
                    + _ack()
                    + _frame(startup.VERDICT, status=startup.COMMITTED),
                    blocking_close=True,
                ),
            ),
            (False, _Pipe),
        ],
        ids=["windows-read-over", "posix-read-parked"],
    )
    def test_the_caller_closes_the_pipe_it_may_take(
        self,
        windows: bool,
        make_pipe: Callable[[], _Pipe],
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Only Windows keeps a read in flight; every other case closes here, once."""
        monkeypatch.setattr(startup, "_IS_WINDOWS", windows)
        pipe = make_pipe()
        before = set(threading.enumerate())
        channel = startup.FrontendChannel(_Child(pipe), _NONCE)
        readers = [thread for thread in threading.enumerate() if thread not in before]

        if windows:
            assert channel.receive(timeout=_PARKED) is not None
            assert channel.receive(timeout=_PARKED) is not None
        else:
            with pytest.raises(TimeoutError):
                channel.receive(timeout=0.05)
        assert pipe.reading.wait(_PARKED)

        channel.close()
        for reader in readers:
            reader.join(_PARKED)

        assert (pipe.detaches, pipe.closes) == (1, 1)


#: A stand-in owner, run as a script by the real interpreter in place of
#: ``python -m linkedin_mcp_server.daemon.owner``. It speaks raw bytes on its own
#: standard streams rather than through ``OwnerChannel``, so it can say what a
#: genuine owner never would. Arguments: mode, fault, output directory.
_STAND_IN_OWNER = r"""
import os
import struct
import sys
import time
from pathlib import Path

from linkedin_mcp_server.daemon import startup, version

mode, fault, out = sys.argv[1], sys.argv[2], Path(sys.argv[3])


def exactly(size):
    data = b""
    while len(data) < size:
        chunk = os.read(0, size - len(data))
        if not chunk:
            raise SystemExit(3)
        data += chunk
    return data


def frame():
    (size,) = struct.unpack("!I", exactly(4))
    return startup.decode(exactly(size))


def record_until_eof():
    # Everything else the frontend sends, as it arrives, so the test can see
    # what crossed even though this process is killed rather than exiting.
    sink = os.open(out / "received", os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    while chunk := os.read(0, 65536):
        os.write(sink, chunk)


FAULTS = {
    "oversize": struct.pack("!I", startup.MAX_FRAME_BYTES + 1),
    "zero": struct.pack("!I", 0),
    "garbage": struct.pack("!I", 4) + b"\xff\xfe{]",
    "partial": struct.pack("!I", 64) + b'{"type":',
}

(out / "imported").write_text("imported")
if mode == "silent":
    time.sleep(600)
if mode == "eof-reader":
    record_until_eof()
    time.sleep(600)

nonce = startup.expect(frame(), startup.HELLO, nonce=None)["nonce"]
os.write(1, startup.sync_line(nonce))
build = version.build_identity()
if mode == "other-build":
    build = "someone-else:0.0.0:sha256:" + "0" * 64
if mode in ("other-build", "other-protocol"):
    protocol = startup.PROTOCOL + (1 if mode == "other-protocol" else 0)
    os.write(
        1,
        startup.encode(
            startup.message(startup.ACK, nonce, protocol=protocol, build=build)
        ),
    )
    record_until_eof()
    time.sleep(600)
if mode == "after-config":
    os.write(
        1,
        startup.encode(
            startup.message(
                startup.ACK, nonce, protocol=startup.PROTOCOL, build=build
            )
        ),
    )
    startup.expect(frame(), startup.CONFIG, nonce=nonce)
    (out / "configured").write_text("configured")
os.write(1, FAULTS[fault])
if fault == "partial":
    os.close(1)
time.sleep(600)
"""


def _await_imports(child: subprocess.Popen[Any], imported: Path) -> None:
    """Hold the spawn until the stand-in has imported the server.

    The startup budget is for the exchange, not for an interpreter loading the
    package under a loaded test run.
    """
    deadline = time.monotonic() + 30.0
    while not imported.exists():
        assert child.poll() is None, "the stand-in owner exited during its imports"
        assert time.monotonic() < deadline, "the stand-in owner never imported"
        time.sleep(0.01)


class _StandIns:
    """Stand-in owners started in place of the real one, and what they saw."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.script = tmp_path / "stand_in_owner.py"
        self.script.write_text(_STAND_IN_OWNER)
        self.out = tmp_path / "stand-in"
        self.out.mkdir()
        self.children: list[subprocess.Popen[Any]] = []
        self.mode = ""
        self.fault = "-"
        real = subprocess.Popen

        def substitute(command: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
            if command[1:4] != ["-P", "-m", "linkedin_mcp_server.daemon.owner"]:
                return real(command, **kwargs)
            child = real(
                [command[0], str(self.script), self.mode, self.fault, str(self.out)],
                **kwargs,
            )
            self.children.append(child)
            _await_imports(child, self.out / "imported")
            return child

        monkeypatch.setattr(election_module.subprocess, "Popen", substitute)

    def received(self) -> list[str]:
        """The kinds of the frames the stand-in read after HELLO."""
        path = self.out / "received"
        wire = io.BytesIO(path.read_bytes() if path.exists() else b"")
        kinds: list[str] = []
        while True:
            try:
                kinds.append(cast(str, startup.read_frame(wire.read)["type"]))
            except EOFError:
                return kinds

    def raw_received(self) -> bytes:
        path = self.out / "received"
        return path.read_bytes() if path.exists() else b""

    def stop(self) -> None:
        for child in self.children:
            if child.poll() is None:  # pragma: no cover - the spawn stopped it
                child.kill()
                child.wait(timeout=30)


def _secret_config(profile: Path) -> AppConfig:
    config = _config(profile)
    config.browser.proxy_server = "http://proxy.example:8080"
    config.browser.proxy_username = "user"
    config.browser.proxy_password = "the-proxy-password"
    return config


def _assert_nothing_survived(stand_ins: _StandIns, auth_root: Path) -> None:
    assert [child.returncode for child in stand_ins.children] == [-signal.SIGKILL], (
        "the child the frontend could not trust was not killed"
    )
    assert daemon_descriptor.read(auth_root) is None, "a descriptor was published"
    for thread in threading.enumerate():
        if thread.name.startswith("daemon-startup-"):
            thread.join(timeout=_PARKED)
    assert not any(
        thread.name.startswith("daemon-startup-") for thread in threading.enumerate()
    ), "a startup thread outlived the child it was talking to"


@_POSIX_ONLY
class TestARealChildIsRefused:
    """Every refusal ends in a dead child and nothing published."""

    #: Generous, so a refusal that waited its budget out instead of acting on
    #: what it read shows up as time rather than passing.
    _BUDGET = 20.0
    _PROMPT = 10.0

    @pytest.mark.parametrize("fault", ["oversize", "zero", "garbage", "partial"])
    @pytest.mark.parametrize(
        "mode", ["before-ack", "after-config"], ids=["for-ack", "for-prepared"]
    )
    def test_a_malformed_frame_is_refused_at_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
        fault: str,
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        stand_ins = _StandIns(tmp_path, monkeypatch)
        stand_ins.mode, stand_ins.fault = mode, fault
        began = time.monotonic()
        try:
            outcome = election_module._spawn(
                profile.parent,
                config,
                timeout=self._BUDGET,
                inspector=_DescriptorInspector(profile.parent, profile, config),
            )
            elapsed = time.monotonic() - began
            _assert_nothing_survived(stand_ins, profile.parent)
        finally:
            stand_ins.stop()

        assert outcome is Started.NO
        assert elapsed < self._PROMPT, (
            f"a {fault} frame was waited out for {elapsed:.1f}s instead of refused"
        )
        # The stage the fault stood in for was the one reached.
        assert (stand_ins.out / "configured").exists() is (mode == "after-config")

    @pytest.mark.parametrize("mode", ["silent", "eof-reader"])
    def test_a_child_that_never_answers_is_killed_at_the_deadline(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ):
        # The EOF reader is an owner that frames its input by reading to end of
        # file, as released owners did: it takes HELLO and waits for more.
        profile = _profile(tmp_path)
        config = _secret_config(profile)
        stand_ins = _StandIns(tmp_path, monkeypatch)
        stand_ins.mode = mode
        try:
            outcome = election_module._spawn(
                profile.parent,
                config,
                timeout=0.5,
                inspector=_DescriptorInspector(profile.parent, profile, config),
            )
            _assert_nothing_survived(stand_ins, profile.parent)
        finally:
            stand_ins.stop()

        assert outcome is Started.NO
        assert stand_ins.received() == ([startup.HELLO] if mode == "eof-reader" else [])
        assert b"the-proxy-password" not in stand_ins.raw_received()

    @pytest.mark.parametrize("mode", ["other-build", "other-protocol"])
    def test_another_build_is_refused_before_config_and_falls_back_to_direct(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        mode: str,
    ):
        # Through the whole election, because the fallback is the election's
        # answer: an owner not worth connecting to is what makes the caller run
        # its own browser. One child only, since another would say the same.
        profile = _profile(tmp_path)
        stand_ins = _StandIns(tmp_path, monkeypatch)
        stand_ins.mode = mode
        began = time.monotonic()
        try:
            outcome = obtain_owner(
                profile.parent,
                profile,
                _secret_config(profile),
                deadline_seconds=self._BUDGET,
                settlement_seconds=0,
            )
            elapsed = time.monotonic() - began
            _assert_nothing_survived(stand_ins, profile.parent)
        finally:
            stand_ins.stop()

        assert not outcome.worth_connecting
        assert elapsed < self._PROMPT, (
            "the election kept trying after a terminal answer"
        )
        # Nothing after HELLO reached the child: no CONFIG, so no password.
        assert stand_ins.received() == []
        assert b"the-proxy-password" not in stand_ins.raw_received()


class TestRefusedBeforeSpawning:
    @pytest.fixture
    def no_child(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            election_module.subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("a child was started for a refused start"),
        )

    def test_a_package_changed_since_this_process_started_spawns_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_child: None,
    ):
        profile = _profile(tmp_path)
        daemon_version.build_identity()  # this process's build, frozen already
        monkeypatch.setattr(
            daemon_version,
            "installed_build_identity",
            lambda: "mcp-server-linkedin:9.9.9:sha256:" + "f" * 64,
        )

        outcome = obtain_owner(
            profile.parent,
            profile,
            _config(profile),
            deadline_seconds=5.0,
            settlement_seconds=0,
        )

        assert not outcome.worth_connecting

    def test_an_unreadable_package_spawns_nothing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        no_child: None,
    ):
        profile = _profile(tmp_path)
        daemon_version.build_identity()

        def unreadable() -> str:
            raise PermissionError("the package directory cannot be read")

        monkeypatch.setattr(daemon_version, "installed_build_identity", unreadable)
        config = _config(profile)

        assert (
            election_module._spawn(
                profile.parent,
                config,
                timeout=5.0,
                inspector=_DescriptorInspector(profile.parent, profile, config),
            )
            is Started.ABORTED
        )

    def test_a_configuration_too_large_for_one_frame_spawns_nothing(
        self, tmp_path: Path, no_child: None
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        config.browser.proxy_server = "http://proxy.example:8080"
        config.browser.proxy_bypass = "x" * startup.MAX_FRAME_BYTES

        assert (
            election_module._spawn(
                profile.parent,
                config,
                timeout=5.0,
                inspector=_DescriptorInspector(profile.parent, profile, config),
            )
            is Started.ABORTED
        )


class TestBuildIdentity:
    @pytest.fixture
    def package(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "linkedin_mcp_server"
        (root / "daemon").mkdir(parents=True)
        (root / "__init__.py").write_text("")
        (root / "daemon" / "owner.py").write_text("SERVE = True\n")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00compiled")
        monkeypatch.setattr(linkedin_mcp_server, "__file__", str(root / "__init__.py"))
        return root

    def test_it_names_the_distribution_and_version(self, package: Path):
        identity = daemon_version.installed_build_identity()

        assert identity.startswith(
            f"mcp-server-linkedin:{linkedin_mcp_server.__version__}:"
        )

    @pytest.mark.parametrize(
        "change",
        [
            lambda root: (root / "daemon" / "owner.py").write_text("SERVE = False\n"),
            lambda root: (root / "daemon" / "new.py").write_text(""),
            lambda root: (root / "daemon" / "owner.py").rename(
                root / "daemon" / "renamed.py"
            ),
        ],
        ids=["edited", "added", "renamed"],
    )
    def test_a_changed_source_file_changes_it(
        self, package: Path, change: Callable[[Path], object]
    ):
        before = daemon_version.installed_build_identity()
        change(package)

        assert daemon_version.installed_build_identity() != before

    def test_a_package_imported_from_an_archive_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        archive = tmp_path / "build.pyz"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("linkedin_mcp_server/__init__.py", "")
            bundle.writestr("linkedin_mcp_server/daemon/owner.py", "SERVE = True\n")
        monkeypatch.setattr(
            linkedin_mcp_server,
            "__file__",
            str(archive / "linkedin_mcp_server" / "__init__.py"),
        )

        with pytest.raises(OSError):
            daemon_version.installed_build_identity()

    def test_bytecode_caches_do_not(self, package: Path):
        before = daemon_version.installed_build_identity()
        (package / "__pycache__" / "x.cpython-313.pyc").write_bytes(b"\x00recompiled")
        (package / "daemon" / "__pycache__").mkdir()
        (package / "daemon" / "__pycache__" / "owner.pyc").write_bytes(b"\x00")

        assert daemon_version.installed_build_identity() == before

    def test_the_running_build_is_frozen_at_its_first_reading(self, package: Path):
        daemon_version.build_identity.cache_clear()
        try:
            running = daemon_version.build_identity()
            (package / "daemon" / "owner.py").write_text("SERVE = False\n")

            assert daemon_version.build_identity() == running
            assert daemon_version.installed_build_identity() != running
        finally:
            # Read again from the real package by whoever asks next.
            daemon_version.build_identity.cache_clear()


class TestTheOwnersEnd:
    def test_a_hello_without_a_valid_nonce_is_answered_with_nothing(self):
        frontend = _Frontend(
            _frame(startup.HELLO, "not-a-nonce", protocol=startup.PROTOCOL)
        )
        try:
            with pytest.raises(startup.ProtocolError):
                frontend.channel.accept(build=daemon_version.build_identity())
            frontend.channel.close_output()
            received = frontend.received()
        finally:
            frontend.close()

        assert received == b""

    def test_the_owner_answers_hello_with_its_sync_line_and_build(self):
        config = AppConfig()
        frontend = _Frontend(_greeting(config))
        try:
            nonce, payload = frontend.channel.accept(build="this-build")
            frontend.channel.close_output()
            received = frontend.received()
        finally:
            frontend.close()

        assert nonce == _NONCE
        assert payload == daemon_config.encode(config)
        assert received == startup.sync_line(_NONCE) + startup.encode(
            startup.message(
                startup.ACK, _NONCE, protocol=startup.PROTOCOL, build="this-build"
            )
        )

    @_POSIX_ONLY
    def test_nothing_else_in_the_owner_reaches_the_protocol_pipe(self, tmp_path: Path):
        # A print, a raw write to descriptor 1 and a child process that
        # inherits it, all after the claim. Each would sit between two frames
        # and end the exchange. A read of standard input must not take bytes
        # meant for the protocol either.
        script = (
            "import os, subprocess, sys\n"
            "from linkedin_mcp_server.daemon import startup\n"
            "channel = startup.OwnerChannel.claim()\n"
            "print('stray print', flush=True)\n"
            "os.write(1, b'stray write\\n')\n"
            "subprocess.run([sys.executable, '-c', \"print('stray grandchild')\"])\n"
            "stray = sys.stdin.buffer.read()\n"
            "hello = channel.read()\n"
            "status = startup.COMMITTED if stray == b'' else startup.FAILED\n"
            "channel.send(startup.VERDICT, hello['nonce'], status=status)\n"
        )
        hello = _frame(startup.HELLO, protocol=startup.PROTOCOL)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            input=hello,
            capture_output=True,
            timeout=60,
            cwd=tmp_path,
        )

        assert completed.returncode == 0, completed.stderr.decode(errors="replace")
        assert completed.stdout == _frame(startup.VERDICT, status=startup.COMMITTED)
        for stray in (b"stray print", b"stray write", b"stray grandchild"):
            assert stray in completed.stderr


@pytest.mark.skipif(
    os.name == "nt",
    reason="the owner entry point takes a Job Object handoff on Windows",
)
class TestOwnerFinalization:
    """What the owner's ``finally`` owes the next election.

    ``daemon.owner.main`` closes its startup channel before it releases the
    daemon lock. A close that waited for the commit read parked in the channel
    would be a lock never released: the process has already decided not to
    serve, and every later election would contend with a corpse that still
    holds the position.
    """

    def test_an_unauthorized_owner_frees_the_lock_it_holds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import lock as daemon_lock
        from linkedin_mcp_server.daemon import owner as daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        profile = tmp_path / "profile"
        profile.mkdir()
        config = AppConfig()
        config.browser.user_data_dir = str(profile)

        class SilentBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                pass

            def attached(self, _log_path: Path, _nonce: str) -> None:
                pass

            def close(self) -> None:
                pass

        # A real channel over real pipes, and this test is the frontend that
        # greets, stays open and never commits.
        frontend = _Frontend(_greeting(config), keep_open=True)
        held: list[daemon_lock.DaemonLock] = []

        def take_lock(auth_root: Path) -> daemon_lock.DaemonLock:
            lock = daemon_lock.DaemonLock(auth_root)
            assert lock.try_acquire()
            held.append(lock)
            return lock

        authorization_timed_out = threading.Event()

        async def stalled_serve(**kwargs: Any) -> int:
            # Production authorization, production read. COMMIT never comes, so
            # ``_read_control_until`` gives up with its reader thread still
            # inside the pipe, which is the state the owner's own finalization
            # then has to close through.
            with pytest.raises(TimeoutError):
                await daemon_owner._read_control_until(
                    kwargs["control"], time.monotonic() + 0.3
                )
            authorization_timed_out.set()
            return 1

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", SilentBootstrap)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner, "_claim_startup_channel", lambda: frontend.channel
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda _profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner, "_attach_daemon_log", lambda _root: tmp_path / "daemon.log"
        )
        monkeypatch.setattr(daemon_owner, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(daemon_owner, "_take_lock", take_lock)
        monkeypatch.setattr(daemon_owner, "_serve", stalled_serve)

        status: list[int] = []
        runner = threading.Thread(
            target=lambda: status.append(daemon_owner.main([])),
            name="owner-main",
            daemon=True,
        )
        runner.start()
        try:
            runner.join(timeout=20.0)

            # Bounded on purpose. A close that waits for the parked read does
            # not fail an assertion about ordering, it stops `main` from ever
            # returning, and a test without a bound would hang the suite.
            assert not runner.is_alive(), (
                "the owner never finished finalizing, so it still holds the lock"
            )
        finally:
            # The frontend goes away, which is what finally ends the parked read.
            frontend.end_input()
        assert authorization_timed_out.is_set()
        assert status == [1]
        assert held, "the test never took the lock it is asserting about"

        acquired: list[bool] = []
        contender = daemon_lock.DaemonLock(tmp_path)
        successor = threading.Thread(
            target=lambda: acquired.append(contender.try_acquire()), daemon=True
        )
        successor.start()
        successor.join(timeout=10.0)
        try:
            assert acquired == [True], (
                "the position is still held by an owner that gave up"
            )
        finally:
            contender.release()
            frontend.close()
