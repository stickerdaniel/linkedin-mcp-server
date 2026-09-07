"""Handing an owner the configuration it has to agree on.

The owner is a separate process that opens a browser against a logged-in
session, and it cannot work out its own settings: half of them arrived on the
command line of the *frontend*. Get this wrong in the quiet direction and the
result is not a visible error but a client that elects an owner and then refuses
to use it, because the fingerprint it compares covers exactly these fields.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import fields
from pathlib import Path
from typing import Any, cast

import pytest

from linkedin_mcp_server import daemon_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import config_fingerprint

_NONCE = "0123456789abcdef" * 4
_CONTROL = daemon_config.ControlEndpoint("127.0.0.1", 54321)


def _config(**browser: object) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = "~/somewhere/profile"
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


class TestRoundTrip:
    def test_the_owner_agrees_with_the_frontend_that_started_it(self):
        # The property that matters, stated the way the daemon uses it: the
        # fingerprint is what a client compares before attaching, so an owner
        # rebuilt from this must produce the same one. Anything dropped in
        # transit shows up here as a client that will not talk to the owner it
        # just started.
        original = _config(
            headless=False,
            user_agent="Mozilla/5.0 (test)",
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            viewport_width=1920,
            login_timeout_seconds=900.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        key = "a-token"
        assert config_fingerprint(restored, key=key) == config_fingerprint(
            original, key=key
        )

    def test_owner_handover_carries_the_post_spawn_nonce(self):
        original = _config(headless=False)

        handover = daemon_config.decode_handover(
            daemon_config.encode_handover(original, _NONCE, _CONTROL)
        )

        assert handover.handshake_nonce == _NONCE
        assert handover.config.browser.headless is False
        assert handover.startup_protocol == daemon_config.STARTUP_PROTOCOL_VERSION

    def test_missing_startup_protocol_means_the_predecessor_frontend(self):
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload.pop("startup_protocol")
        payload.pop("control_host")
        payload.pop("control_port")

        handover = daemon_config.decode_handover(json.dumps(payload))

        assert handover.startup_protocol == 1
        assert handover.control is None

    @pytest.mark.parametrize("value", [True, 0, 4, "2"])
    def test_unknown_startup_protocol_is_refused(self, value: object):
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload["startup_protocol"] = value

        with pytest.raises(ValueError, match="startup protocol"):
            daemon_config.decode_handover(json.dumps(payload))

    def test_the_current_record_names_the_control_rendezvous(self):
        handover = daemon_config.decode_handover(
            daemon_config.encode_handover(_config(), _NONCE, _CONTROL)
        )

        assert handover.startup_protocol == 3
        assert handover.control == _CONTROL

    def test_a_current_record_without_a_rendezvous_is_refused(self):
        # Nothing could authorize such an owner: it would prepare a generation
        # and then wait out its commit deadline with the lock in hand.
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload.pop("control_host")

        with pytest.raises(ValueError, match="control host"):
            daemon_config.decode_handover(json.dumps(payload))

    @pytest.mark.parametrize("port", [0, 65536, -1, True, "8080", 8080.0])
    def test_an_unusable_control_port_is_refused(self, port: object):
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload["control_port"] = port

        with pytest.raises(ValueError, match="control port"):
            daemon_config.decode_handover(json.dumps(payload))

    @pytest.mark.parametrize("protocol", [1, 2])
    def test_an_older_protocol_may_not_name_a_rendezvous(self, protocol: int):
        # A frontend on either predecessor protocol never listens, so a record
        # naming one did not come from a frontend that would answer it.
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload["startup_protocol"] = protocol

        with pytest.raises(ValueError, match="control channel"):
            daemon_config.decode_handover(json.dumps(payload))

    def test_the_pipe_commit_protocol_still_decodes(self):
        # The version between the two: commit authorization on the configuration
        # pipe. An owner from this build can still be started by a frontend that
        # speaks it, which is the rollback case in the other direction.
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))
        payload["startup_protocol"] = 2
        del payload["control_host"], payload["control_port"]

        handover = daemon_config.decode_handover(json.dumps(payload))

        assert handover.startup_protocol == 2
        assert handover.control is None
        assert daemon_config.authorizes_commit(handover.startup_protocol)

    def test_only_the_predecessor_protocol_publishes_unauthorized(self):
        assert not daemon_config.authorizes_commit(1)
        assert daemon_config.authorizes_commit(2)
        assert daemon_config.authorizes_commit(3)

    def test_the_rendezvous_stays_outside_the_sections_a_predecessor_parses(self):
        # What makes a current record readable by an owner that has never heard
        # of a control channel. Every version of the decoder reads ``browser``
        # and ``server`` by name and refuses an unknown setting *inside* them,
        # while ignoring top-level names it does not know.
        payload = json.loads(daemon_config.encode_handover(_config(), _NONCE, _CONTROL))

        assert {"control_host", "control_port", "startup_protocol"} <= set(payload)
        assert not {"control_host", "control_port"} & set(payload["browser"])
        assert not {"control_host", "control_port"} & set(payload["server"])
        # And the predecessor's own reconstruction, which reads exactly these
        # three names, still finds everything it needs.
        assert {"browser", "server", "handshake_nonce"} <= set(payload)

    def test_owner_handover_requires_a_valid_nonce(self):
        with pytest.raises(ValueError, match="handshake nonce"):
            daemon_config.decode_handover(daemon_config.encode(_config()))

    def test_every_browser_setting_crosses_unchanged(self):
        # The fingerprint check above is necessary and not sufficient: it only
        # covers SHARED_CONFIG_FIELDS, and a field left at its default produces
        # the same digest whether it crossed or was dropped. So the whole
        # section is compared field by field, which is what would actually
        # notice a setting quietly going missing from the codec.
        original = _config(
            headless=False,
            slow_mo=25,
            user_agent="Mozilla/5.0 (test)",
            viewport_width=1920,
            viewport_height=1080,
            default_timeout=9000,
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            proxy_bypass="localhost",
            login_timeout_seconds=900.0,
            login_inline_wait_seconds=10.0,
            browser_wait_seconds=30.0,
            browser_min_hold_seconds=5.0,
            browser_idle_timeout_seconds=120.0,
            auto_import_from_browser=False,
            eager_full_chromium=True,
        )

        carried = daemon_config.encode(original)
        restored = daemon_config.decode(carried)

        assert restored.browser == original.browser
        # And every field was actually sent, not merely restored to a default
        # that happened to match. The comparison above cannot tell those apart
        # for any field left at its default — `chrome_path` is one, and so is
        # every field added in future. Checking the serialised keys against the
        # dataclass is what makes the name of this test true.
        assert set(json.loads(carried)["browser"]) == {
            field.name for field in fields(original.browser)
        }

    def test_settings_outside_the_fingerprint_survive_too(self):
        # The fingerprint says which differences stop two clients sharing an
        # owner. It does not say which settings the owner needs to do its job:
        # `browser_wait_seconds` and `browser_min_hold_seconds` are not in
        # SHARED_CONFIG_FIELDS at all, so an owner left on their defaults would
        # hand the browser around on a schedule the user did not choose while
        # every fingerprint still matched. (`browser_idle_timeout_seconds` *is*
        # in that list and is included here only as a third value to carry.)
        #
        # The values chosen survive validate() untouched, so this pins the
        # transport rather than the clamping: the wait has a 45 second ceiling
        # and the minimum hold has to leave room inside it, and picking values
        # those clamps act on would make this a test of the validator.
        original = _config(
            browser_wait_seconds=30.0,
            browser_min_hold_seconds=5.0,
            browser_idle_timeout_seconds=120.0,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.browser.browser_wait_seconds == 30.0
        assert restored.browser.browser_min_hold_seconds == 5.0
        assert restored.browser.browser_idle_timeout_seconds == 120.0

    def test_the_tool_timeout_crosses_because_the_owner_enforces_it(self):
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.tool_timeout_seconds = 42.5

        restored = daemon_config.decode(daemon_config.encode(original))

        assert restored.server.tool_timeout_seconds == 42.5


class TestDaemonLogState:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
    def test_fresh_log_state_is_private_under_umask_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_descriptor, daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        monkeypatch.setattr(daemon_owner.os, "dup2", lambda *args: None)
        stream = type("Stream", (), {"fileno": lambda self: 1})()
        monkeypatch.setattr(
            daemon_owner,
            "sys",
            type("Streams", (), {"stdout": stream, "stderr": stream})(),
        )
        previous = os.umask(0)
        try:
            log_path = daemon_owner._attach_daemon_log(tmp_path / "auth")
        finally:
            os.umask(previous)

        assert (
            stat.S_IMODE(daemon_descriptor.daemon_state_root().stat().st_mode) == 0o700
        )
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name != "nt", reason="Windows ACLs are required")
    def test_log_file_uses_the_private_file_acl(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_descriptor, daemon_owner
        from linkedin_mcp_server.windows_acl import describe_dacl

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        monkeypatch.setattr(daemon_owner.os, "dup2", lambda *args: None)
        stream = type("Stream", (), {"fileno": lambda self: 1})()
        monkeypatch.setattr(
            daemon_owner,
            "sys",
            type("Streams", (), {"stdout": stream, "stderr": stream})(),
        )

        log_path = daemon_owner._attach_daemon_log(tmp_path / "auth")

        described = describe_dacl(log_path)
        assert described.protected is True
        assert len(described.entries) == 1

    def test_windows_log_is_hardened_before_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_owner

        log_path = tmp_path / "daemon.log"
        hardened: list[Path] = []
        verified: list[Path] = []
        real_harden = daemon_owner.harden_created_file

        def harden(staged: Path) -> None:
            assert staged.parent == log_path.parent
            assert staged != log_path
            assert not log_path.exists()
            hardened.append(staged)
            real_harden(staged)

        monkeypatch.setattr(daemon_owner, "harden_created_file", harden)
        monkeypatch.setattr(
            daemon_owner, "harden_file", lambda path: verified.append(path)
        )

        daemon_owner._publish_windows_daemon_log(log_path)

        assert len(hardened) == 1
        assert not hardened[0].exists()
        assert verified == [log_path]
        assert log_path.is_file()

    def test_a_published_windows_log_survives_a_failed_verification(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Publication hands the file over, so the publisher stops owning it.

        Another candidate can open the published inode before the verification
        below fails, and withdrawing the path then unlinks a log that is
        already being written to.
        """
        from linkedin_mcp_server import daemon_owner
        from linkedin_mcp_server.private_state import PrivateStateError

        log_path = tmp_path / "daemon.log"
        monkeypatch.setattr(
            daemon_owner,
            "harden_file",
            lambda _path: (_ for _ in ()).throw(PrivateStateError("ACL failure")),
        )

        with pytest.raises(PrivateStateError, match="ACL failure"):
            daemon_owner._publish_windows_daemon_log(log_path)

        assert log_path.is_file()
        assert not list(tmp_path.glob(".daemon-log-*")), "and nothing stayed staged"

    def test_failed_fresh_log_hardening_leaves_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A candidate that fails does not take the log away from one that did not.

        Its own creation may have lost the race by a syscall, in which case
        another candidate is already appending to this inode and nothing here
        can tell. The file was opened owner-only, so leaving it costs nothing
        and the next attachment hardens it through the existing-file path.
        """
        from linkedin_mcp_server import daemon_descriptor, daemon_owner
        from linkedin_mcp_server.private_state import PrivateStateError

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "harden_created_file",
            lambda _path: (_ for _ in ()).throw(PrivateStateError("ACL failure")),
        )

        with pytest.raises(PrivateStateError, match="ACL failure"):
            daemon_owner._attach_daemon_log(tmp_path / "auth")

        log_path = daemon_owner.daemon_log_path(tmp_path / "auth")
        assert log_path.is_file()
        assert stat.S_IMODE(log_path.stat().st_mode) & 0o077 == 0

    @pytest.mark.skipif(os.name == "nt", reason="POSIX open flags decide this")
    def test_a_lost_creation_race_leaves_the_winners_log_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Two candidates can both find the path absent, and one has to yield.

        A candidate that did not create the file may not remove it again, or
        its own failure unlinks the log the candidate that went on to win the
        daemon lock is already writing to.
        """
        from linkedin_mcp_server import daemon_descriptor, daemon_owner
        from linkedin_mcp_server.private_state import PrivateStateError

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        monkeypatch.setattr(daemon_owner.os, "dup2", lambda *args: None)
        stream = type("Stream", (), {"fileno": lambda self: 1})()
        monkeypatch.setattr(
            daemon_owner,
            "sys",
            type("Streams", (), {"stdout": stream, "stderr": stream})(),
        )
        monkeypatch.setattr(
            daemon_owner,
            "harden_created_file",
            lambda _path: (_ for _ in ()).throw(PrivateStateError("ACL failure")),
        )

        log_path = daemon_owner.daemon_log_path(tmp_path / "auth")
        real_open = os.open
        raced: list[bool] = []

        def racing_open(path, flags, mode=0o777, **rest):
            if Path(path) == log_path and not raced:
                raced.append(True)
                # The other candidate creates the file between this one's
                # absence check and its own open.
                log_path.write_bytes(b"winner\n")
            return real_open(path, flags, mode, **rest)

        monkeypatch.setattr(daemon_owner.os, "open", racing_open)

        attached = daemon_owner._attach_daemon_log(tmp_path / "auth")

        assert raced, "the creation race was actually exercised"
        assert attached == log_path
        assert log_path.read_bytes() == b"winner\n"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are required")
    def test_a_planted_log_symlink_is_refused_before_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_descriptor, daemon_owner
        from linkedin_mcp_server.private_state import PrivateStateError

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        directory = daemon_descriptor.daemon_dir(tmp_path / "auth")
        directory.mkdir(parents=True, mode=0o777)
        directory.chmod(0o777)
        victim = tmp_path / "victim.log"
        victim.write_text("untouched")
        victim.chmod(0o644)
        (directory / "daemon.log").symlink_to(victim)

        with pytest.raises(PrivateStateError, match="symbolic link"):
            daemon_owner._attach_daemon_log(tmp_path / "auth")

        assert victim.read_text() == "untouched"
        assert stat.S_IMODE(victim.stat().st_mode) == 0o644

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
    def test_an_existing_regular_log_is_hardened_before_append(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_descriptor, daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        monkeypatch.setattr(daemon_owner.os, "dup2", lambda *args: None)
        stream = type("Stream", (), {"fileno": lambda self: 1})()
        monkeypatch.setattr(
            daemon_owner,
            "sys",
            type("Streams", (), {"stdout": stream, "stderr": stream})(),
        )
        directory = daemon_descriptor.daemon_dir(tmp_path / "auth")
        directory.mkdir(parents=True, mode=0o777)
        directory.chmod(0o777)
        log_path = directory / "daemon.log"
        log_path.write_text("existing\n")
        log_path.chmod(0o666)

        attached = daemon_owner._attach_daemon_log(tmp_path / "auth")

        assert attached == log_path
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


class TestRefusing:
    def test_the_frontends_own_invocation_does_not_cross(self):
        # An owner that adopted the frontend's transport would try to serve
        # stdio, and one that adopted its one-shot flags would re-run the
        # client's --login. Those describe how *that* process was started.
        original = AppConfig()
        original.browser.user_data_dir = "~/p/profile"
        original.server.login = True
        original.server.transport = "streamable-http"
        original.server.port = 9999

        carried = json.loads(daemon_config.encode(original))

        assert "login" not in carried["server"]
        assert "transport" not in carried["server"]
        assert "port" not in carried["server"]

    def test_an_unknown_setting_is_refused_rather_than_skipped(self):
        # Both ends are one installation, so a name this does not recognise did
        # not come from encode(). Skipping it quietly would be an owner running
        # on settings nobody chose.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {"nonsense": 1}, "server": {}}))

    def test_a_frontend_setting_smuggled_into_the_server_section_is_refused(self):
        # The allowed list is enforced on the way in, not only on the way out.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode(json.dumps({"browser": {}, "server": {"login": True}}))

    def test_a_value_of_the_wrong_type_is_refused_before_it_reaches_a_browser(self):
        # JSON cannot tell seconds from a count. Left unchecked this surfaces
        # deep inside a browser launch, in a detached process, as an error
        # nobody can trace back to here.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": "quickly"}, "server": {}})
            )

    def test_a_boolean_is_not_accepted_where_a_number_belongs(self):
        # bool is a subclass of int, so an isinstance check alone accepts True
        # as a timeout. That is exactly the silent nonsense this refuses.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {"slow_mo": True}, "server": {}})
            )

    def test_an_integer_is_accepted_where_a_float_belongs(self):
        # The other direction, and it has to work: JSON writes 0 for 0.0, so
        # refusing would make an ordinary configuration untransportable.
        restored = daemon_config.decode(
            json.dumps({"browser": {"login_timeout_seconds": 0}, "server": {}})
        )

        assert restored.browser.login_timeout_seconds == 0

    def test_an_optional_setting_may_be_unset(self):
        restored = daemon_config.decode(
            json.dumps({"browser": {"user_agent": None}, "server": {}})
        )

        assert restored.browser.user_agent is None

    def test_a_log_level_outside_the_supported_set_is_refused(self):
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"log_level": "CHATTY"}})
            )

    def test_a_configuration_the_frontend_would_reject_is_rejected_here_too(self):
        # The owner is the process that opens the browser, so a value refused at
        # the frontend must not reach Chromium through this back door.
        #
        # The specific error, not a bare Exception: this is asserting that
        # `validate()` ran, and any decoder bug at all would satisfy a looser
        # check while the validation it names had quietly stopped happening.
        from linkedin_mcp_server.config.schema import ConfigurationError

        with pytest.raises(ConfigurationError, match="tool_timeout_seconds"):
            daemon_config.decode(
                json.dumps({"browser": {}, "server": {"tool_timeout_seconds": -1.0}})
            )

    def test_the_owner_applies_the_browser_mode_it_was_handed(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Installing the configuration is not enough. The browser mode lives in
        # a module global that defaults to headless, and `_make_browser` reads
        # that global rather than the configuration — the frontend's own entry
        # point sets it explicitly for exactly this reason.
        #
        # Without it, an owner started with `--no-headless` publishes a
        # fingerprint saying so, the frontend compares it and attaches happily,
        # and the browser opens headless anyway: no window, no error, and a user
        # who asked to watch it left wondering.
        #
        # Driven through `main`, and asserted against the global the launcher
        # actually reads. Calling `set_headless` directly here would only prove
        # that setter works; what was broken is that the owner never called it.
        import io
        import sys

        import linkedin_mcp_server.drivers.browser as browser_module
        from linkedin_mcp_server import daemon_owner
        from linkedin_mcp_server.process_control import ControlListener

        visible = AppConfig()
        visible.browser.user_data_dir = "~/p/profile"
        visible.browser.headless = False

        original = browser_module.current_headless()
        stdin = sys.stdin
        browser_module.set_headless(True)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: auth_root / "daemon.log",
        )
        # A real rendezvous, so the owner gets past its control attach and on to
        # the lock. Nothing here accepts it: the connection waits in the backlog,
        # exactly as it does until a parent has a prepared generation to validate.
        control = ControlListener.open()
        sys.stdin = io.StringIO(
            daemon_config.encode_handover(
                visible,
                _NONCE,
                daemon_config.ControlEndpoint(control.host, control.port),
            )
            + "\n"
        )
        try:
            # It gets as far as taking the lock, which fails on the deliberately
            # invalid descriptor and is reported rather than raised. That is
            # well past the point where the browser mode is settled.
            assert daemon_owner.main(["--lock-fd", "-1"]) == 1

            assert browser_module.current_headless() is False, (
                "the owner ignored the browser mode it was handed"
            )
        finally:
            control.close()
            sys.stdin = stdin
            browser_module.set_headless(original)

    def test_owner_verdict_carries_the_handed_over_nonce(self):
        from linkedin_mcp_server import daemon_owner

        class RecordingStream:
            def __init__(self) -> None:
                self.written = ""
                self.flushed = False
                self.closed = False

            def write(self, value: str) -> int:
                self.written += value
                return len(value)

            def flush(self) -> None:
                self.flushed = True

            def close(self) -> None:
                self.closed = True

        stream = RecordingStream()
        daemon_owner._Handshake(cast(Any, stream), _NONCE).committed()

        assert stream.written == f"owner {_NONCE} committed\n"
        assert stream.flushed
        assert stream.closed

    def test_a_configuration_failure_reports_then_closes_without_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Startup output can already contain status-shaped lines. Until the
        # configuration yields the post-spawn nonce, the owner has no frame it can
        # authenticate, so failure is represented by a fixed diagnostic and EOF.
        from linkedin_mcp_server import daemon_owner

        events: list[str] = []

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        class RecordingStream:
            def close(self) -> None:
                events.append("closed")

        def reject() -> daemon_config.OwnerHandover:
            raise ValueError("invalid handed-over configuration")

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner, "_claim_handshake_stream", lambda: RecordingStream()
        )
        monkeypatch.setattr(daemon_owner, "_read_handover", reject)
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *_args, **_kwargs: pytest.fail("bootstrap failure used logging"),
        )

        assert daemon_owner.main([]) == 1

        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_CONFIGURATION}",
            "closed",
        ]

    @pytest.mark.parametrize(
        ("stage", "code"),
        [
            ("state", "state"),
            ("log", "log"),
        ],
    )
    def test_prelog_failures_use_the_fixed_bootstrap_channel(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stage: str,
        code: str,
    ):
        from linkedin_mcp_server import daemon_owner

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, candidate: str) -> None:
                events.append(f"diagnostic:{candidate}")

            def close(self) -> None:
                pass

        class RecordingHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def abort(self) -> None:
                events.append("aborted")

            def close(self) -> None:
                pass

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", RecordingHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_handshake_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *_args, **_kwargs: pytest.fail("pre-log failure used logging"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_abandon_inherited_lock",
            lambda fd: events.append(f"unlocked:{fd}"),
        )
        if stage == "state":

            class UnresolvablePath:
                def expanduser(self) -> UnresolvablePath:
                    return self

                def resolve(self) -> Path:
                    raise OSError("profile state is unavailable")

            monkeypatch.setattr(daemon_owner, "Path", lambda _value: UnresolvablePath())
        else:
            monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
            monkeypatch.setattr(
                daemon_owner,
                "_attach_daemon_log",
                lambda auth_root: (_ for _ in ()).throw(
                    OSError("daemon log is unavailable")
                ),
            )

        assert daemon_owner.main(["--lock-fd", "123"]) == 1
        assert events == ["unlocked:123", f"diagnostic:{code}", "aborted"]

    def test_predecessor_windows_frontend_is_refused_before_state_access(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_owner

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        class RecordingHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def fail(self) -> None:
                events.append("failed")

            def close(self) -> None:
                events.append("closed")

        monkeypatch.setattr(daemon_owner, "_IS_WINDOWS", True)
        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", RecordingHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_handshake_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda: daemon_config.OwnerHandover(config, _NONCE, startup_protocol=1),
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda _profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda _auth_root: pytest.fail("predecessor reached the v2 state root"),
        )

        assert daemon_owner.main([]) == 1
        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_STATE}",
            "failed",
            "closed",
        ]

    def test_lock_adoption_failure_unlocks_the_inherited_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_owner
        from linkedin_mcp_server.daemon_lock import DaemonLockError

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                events.append("bootstrap-closed")

        class RecordingHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def abort(self) -> None:
                events.append("aborted")

            def close(self) -> None:
                events.append("handshake-closed")

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", RecordingHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_handshake_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: tmp_path / "daemon.log",
        )
        monkeypatch.setattr(daemon_owner, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda *args: (_ for _ in ()).throw(DaemonLockError("cannot adopt")),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_abandon_inherited_lock",
            lambda fd: events.append(f"unlocked:{fd}"),
        )
        monkeypatch.setattr(daemon_owner.logger, "exception", lambda *args: None)

        assert daemon_owner.main(["--lock-fd", "123"]) == 1
        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_ATTACHED}",
            "unlocked:123",
            "aborted",
            "bootstrap-closed",
            "handshake-closed",
        ]

    def test_log_attachment_closes_bootstrap_with_an_actionable_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_owner

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        class RecordingHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def retry(self) -> None:
                events.append("retry")

            def close(self) -> None:
                pass

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", RecordingHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_handshake_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: tmp_path / "daemon.log",
        )
        monkeypatch.setattr(daemon_owner, "configure_logging", lambda **kwargs: None)
        monkeypatch.setattr(daemon_owner, "_take_lock", lambda *args: None)

        assert daemon_owner.main([]) == 0
        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_ATTACHED}",
            "retry",
        ]

    def test_a_suspended_starter_cannot_pin_the_owner_on_config_read(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        import threading
        import time

        from linkedin_mcp_server import daemon_owner

        release = threading.Event()

        class _SuspendedStarter:
            def readline(self) -> str:
                release.wait()
                return ""

        monkeypatch.setattr(daemon_owner.sys, "stdin", _SuspendedStarter())
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="did not provide its configuration"):
                daemon_owner._read_handover(timeout=0.01)
        finally:
            release.set()

        assert time.monotonic() - started < 1.0

    def test_configuration_timeout_unlocks_the_inherited_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server import daemon_lock, daemon_owner

        if not daemon_lock._INHERITED_LOCKS_TRANSFER:
            pytest.skip("inherited lock handoff is POSIX-only")

        events: list[str] = []

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        class RecordingStream:
            def close(self) -> None:
                events.append("closed")

        parent = daemon_lock.DaemonLock(tmp_path)
        assert parent.try_acquire()
        inherited = parent.inheritable_copy()
        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner, "_claim_handshake_stream", lambda: RecordingStream()
        )
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda: (_ for _ in ()).throw(TimeoutError("starter suspended")),
        )

        try:
            assert daemon_owner.main(["--lock-fd", str(inherited)]) == 1

            contender = daemon_lock.DaemonLock(tmp_path)
            assert contender.try_acquire()
            contender.release()
        finally:
            parent.release()

        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_CONFIGURATION}",
            "closed",
        ]

    def test_the_owner_refuses_to_start_without_a_configuration(self):
        # The owner reads its settings from standard input and must not fall
        # back to parsing its own command line: `load_config` would then see the
        # daemon's argv, and the browser it opened would be configured by
        # accident rather than by the client that asked for it.
        #
        # Exercised through `main`, which is otherwise reached only inside a
        # spawned process where coverage cannot see it.
        import io
        import sys

        from linkedin_mcp_server import daemon_owner

        original = sys.stdin
        sys.stdin = io.StringIO("   \n")
        try:
            assert daemon_owner.main([]) == 1
        finally:
            sys.stdin = original

    def test_something_that_is_not_a_configuration_is_refused(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            daemon_config.decode("not json at all")

        with pytest.raises(ValueError, match="not an object"):
            daemon_config.decode("[1, 2, 3]")

        with pytest.raises(ValueError, match="no browser section"):
            daemon_config.decode(json.dumps({"server": {}}))
