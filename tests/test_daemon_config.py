"""Handing an owner the configuration it has to agree on.

The owner is a separate process that opens a browser against a logged-in
session, and it cannot work out its own settings: half of them arrived on the
command line of the *frontend*. Get this wrong in the quiet direction and the
result is not a visible error but a client that elects an owner and then refuses
to use it, because the fingerprint it compares covers exactly these fields.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from linkedin_mcp_server.daemon import config as daemon_config
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon.descriptor import config_fingerprint

_NONCE = "0123456789abcdef" * 4


class _Frontend:
    """The frontend's ends of two real pipes around an owner's startup channel.

    *sent* is everything the frontend says, written up front and then ended,
    unless *keep_open* holds the input open the way a suspended starter would.
    """

    def __init__(self, sent: bytes = b"", *, keep_open: bool = False) -> None:
        inbound, frontend_input = os.pipe()
        self._input: int | None = frontend_input
        self._output, outbound = os.pipe()
        os.write(self._input, sent)
        if not keep_open:
            self.end_input()
        self.channel = startup.OwnerChannel(inbound, outbound)

    def end_input(self) -> None:
        descriptor, self._input = self._input, None
        if descriptor is not None:
            os.close(descriptor)

    def received(self) -> bytes:
        """Everything the owner wrote, once it has ended its output."""
        data = b""
        while chunk := os.read(self._output, 65536):
            data += chunk
        return data

    def close(self) -> None:
        self.end_input()
        self.channel.close_output()
        self.channel.close()
        os.close(self._output)


def _greeting(config: AppConfig, *, protocol: object = startup.PROTOCOL) -> bytes:
    """HELLO and CONFIG as a frontend of this build writes them."""
    return startup.encode(
        startup.message(startup.HELLO, _NONCE, protocol=protocol)
    ) + startup.encode(
        startup.message(startup.CONFIG, _NONCE, config=daemon_config.encode(config))
    )


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
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
            viewport_width=1920,
        )

        restored = daemon_config.decode(daemon_config.encode(original))

        key = "a-token"
        assert config_fingerprint(restored, key=key) == config_fingerprint(
            original, key=key
        )

    def test_owner_handover_carries_the_post_spawn_nonce(self):
        from linkedin_mcp_server.daemon import owner as daemon_owner

        original = _config(
            headless=False,
            proxy_server="http://proxy.example:8080",
            proxy_username="user",
            proxy_password="secret",
        )
        frontend = _Frontend(_greeting(original))
        try:
            handover = daemon_owner._read_handover(frontend.channel, timeout=5.0)
        finally:
            frontend.close()

        assert handover.handshake_nonce == _NONCE
        assert handover.config.browser.headless is False
        # The one secret in the configuration crosses on the pipe and nowhere
        # else, so it has to arrive intact here.
        assert handover.config.browser.proxy_password == "secret"

    def test_every_browser_setting_crosses_unchanged(self):
        # The fingerprint check above is necessary and not sufficient: it only
        # covers SHARED_CONFIG_FIELDS, and a field left at its default produces
        # the same digest whether it crossed or was dropped. So the whole
        # section is compared field by field, which is what would actually
        # notice a setting quietly going missing from the codec.
        original = _config(
            headless=False,
            slow_mo=25,
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
        )

        carried = daemon_config.encode(original)
        restored = daemon_config.decode(carried)

        assert restored.browser == original.browser
        # And every field was actually sent, not merely restored to a default
        # that happened to match. The comparison above cannot tell those apart
        # for any field left at its default — `chrome_path` is one, and so is
        # every field added in future. Checking the serialised keys against the
        # dataclass is what makes the name of this test true.
        assert set(cast(dict[str, object], carried["browser"])) == {
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


@pytest.fixture
def owner_streams(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give the owner output streams of its own to redirect into the log.

    `_attach_daemon_log` points stdout and stderr at the log with a real
    `os.dup2`. On the runner's own descriptors that would send pytest's output
    into a temporary file, and stubbing `os.dup2` instead replaces it for the
    whole process, so pytest's capture loses the calls that restore its streams.
    """
    from linkedin_mcp_server.daemon import owner as daemon_owner

    with open(os.devnull, "w") as stdout, open(os.devnull, "w") as stderr:
        monkeypatch.setattr(
            daemon_owner, "sys", SimpleNamespace(stdout=stdout, stderr=stderr)
        )
        yield


@pytest.mark.usefixtures("owner_streams")
class TestDaemonLogState:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are required")
    def test_fresh_log_state_is_private_under_umask_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
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
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner
        from linkedin_mcp_server.windows_acl import describe_dacl

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)

        log_path = daemon_owner._attach_daemon_log(tmp_path / "auth")

        described = describe_dacl(log_path)
        assert described.protected is True
        assert len(described.entries) == 1

    def test_windows_log_is_hardened_before_publication(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import owner as daemon_owner

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

        Once published, the path no longer proves which file it names, so
        withdrawing it after the verification below fails could remove a
        foreign entry instead of the log this process staged.
        """
        from linkedin_mcp_server.daemon import owner as daemon_owner
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

    @pytest.mark.skipif(
        os.name == "nt", reason="Windows hardens a staged file before publishing it"
    )
    def test_failed_fresh_log_hardening_leaves_the_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """An owner whose hardening fails leaves the log it created in place.

        The path may no longer name the inode it created, so removing it could
        take a foreign entry. The file was opened owner-only, so leaving it
        costs nothing and the next attachment hardens it through the
        existing-file path.
        """
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner
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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX creates the log in place")
    def test_a_log_planted_after_the_check_fails_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Only the lock holder creates the log, so a late entry is foreign."""
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        log_path = daemon_owner.daemon_log_path(tmp_path / "auth")
        real_open = os.open

        def planting_open(path, flags, mode=0o777, **rest):
            if Path(path) == log_path and not log_path.exists():
                log_path.write_bytes(b"foreign\n")
            return real_open(path, flags, mode, **rest)

        monkeypatch.setattr(daemon_owner.os, "open", planting_open)

        with pytest.raises(FileExistsError):
            daemon_owner._attach_daemon_log(tmp_path / "auth")

        assert log_path.read_bytes() == b"foreign\n"

    @pytest.mark.skipif(os.name != "nt", reason="POSIX rename replaces its target")
    def test_a_windows_log_planted_before_publication_fails_startup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Publication refuses an entry that appeared after the check."""
        from linkedin_mcp_server.daemon import owner as daemon_owner

        log_path = tmp_path / "daemon.log"
        real_harden = daemon_owner.harden_created_file

        def harden_then_plant(staged: Path) -> None:
            real_harden(staged)
            log_path.write_bytes(b"foreign\n")

        monkeypatch.setattr(daemon_owner, "harden_created_file", harden_then_plant)

        with pytest.raises(FileExistsError):
            daemon_owner._publish_windows_daemon_log(log_path)

        assert log_path.read_bytes() == b"foreign\n"
        assert not list(tmp_path.glob(".daemon-log-*")), "and nothing stayed staged"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics are required")
    def test_a_planted_log_symlink_is_refused_before_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner
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
        from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
        from linkedin_mcp_server.daemon import owner as daemon_owner

        monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: tmp_path)
        directory = daemon_descriptor.daemon_dir(tmp_path / "auth")
        directory.mkdir(parents=True, mode=0o777)
        directory.chmod(0o777)
        log_path = directory / "daemon.log"
        log_path.write_text("existing\n")
        log_path.chmod(0o666)

        attached = daemon_owner._attach_daemon_log(tmp_path / "auth")

        assert attached == log_path
        assert log_path.read_text() == "existing\n"
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

        carried = cast(dict[str, Any], daemon_config.encode(original))

        assert "login" not in carried["server"]
        assert "transport" not in carried["server"]
        assert "port" not in carried["server"]

    def test_an_unknown_setting_is_refused_rather_than_skipped(self):
        # Both ends are one installation, so a name this does not recognise did
        # not come from encode(). Skipping it quietly would be an owner running
        # on settings nobody chose.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode({"browser": {"nonsense": 1}, "server": {}})

    def test_a_frontend_setting_smuggled_into_the_server_section_is_refused(self):
        # The allowed list is enforced on the way in, not only on the way out.
        with pytest.raises(ValueError, match="unknown"):
            daemon_config.decode({"browser": {}, "server": {"login": True}})

    def test_a_value_of_the_wrong_type_is_refused_before_it_reaches_a_browser(self):
        # JSON cannot tell seconds from a count. Left unchecked this surfaces
        # deep inside a browser launch, in a detached process, as an error
        # nobody can trace back to here.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode({"browser": {"slow_mo": "quickly"}, "server": {}})

    def test_a_boolean_is_not_accepted_where_a_number_belongs(self):
        # bool is a subclass of int, so an isinstance check alone accepts True
        # as a timeout. That is exactly the silent nonsense this refuses.
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode({"browser": {"slow_mo": True}, "server": {}})

    def test_an_integer_is_accepted_where_a_float_belongs(self):
        # The other direction, and it has to work: JSON writes 0 for 0.0, so
        # refusing would make an ordinary configuration untransportable.
        restored = daemon_config.decode(
            {"browser": {"login_timeout_seconds": 0}, "server": {}}
        )

        assert restored.browser.login_timeout_seconds == 0

    def test_an_optional_setting_may_be_unset(self):
        restored = daemon_config.decode(
            {"browser": {"chrome_path": None}, "server": {}}
        )

        assert restored.browser.chrome_path is None

    def test_a_log_level_outside_the_supported_set_is_refused(self):
        with pytest.raises(ValueError, match="wrong type"):
            daemon_config.decode({"browser": {}, "server": {"log_level": "CHATTY"}})

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
                {"browser": {}, "server": {"tool_timeout_seconds": -1.0}}
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
        import linkedin_mcp_server.drivers.browser as browser_module
        from linkedin_mcp_server.daemon import owner as daemon_owner

        def refuse_lock(*_args):
            raise OSError("lock unavailable")

        monkeypatch.setattr(daemon_owner, "_take_lock", refuse_lock)
        visible = AppConfig()
        visible.browser.headless = False

        original = browser_module.current_headless()
        browser_module.set_headless(True)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: auth_root / "daemon.log",
        )
        frontend = _Frontend(_greeting(visible))
        monkeypatch.setattr(
            daemon_owner, "_claim_startup_channel", lambda: frontend.channel
        )
        try:
            # The lock failure is reported rather than raised, without creating
            # account state. The browser mode must already be settled by then.
            assert daemon_owner.main([]) == 1

            assert browser_module.current_headless() is False, (
                "the owner ignored the browser mode it was handed"
            )
        finally:
            frontend.close()
            browser_module.set_headless(original)

    def test_owner_verdict_carries_the_handed_over_nonce(self):
        import io

        from linkedin_mcp_server.daemon import owner as daemon_owner

        frontend = _Frontend()
        try:
            daemon_owner._Handshake(frontend.channel, _NONCE).committed()
            # The verdict ends the output, so this read reaches end of file.
            received = io.BytesIO(frontend.received())
        finally:
            frontend.close()

        assert startup.read_frame(received.read) == startup.message(
            startup.VERDICT, _NONCE, status=startup.COMMITTED
        )
        assert received.read() == b""

    def test_a_configuration_failure_reports_then_closes_without_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Startup output can already contain status-shaped lines. Until the
        # configuration yields the post-spawn nonce, the owner has no frame it can
        # authenticate, so failure is represented by a fixed diagnostic and EOF.
        from linkedin_mcp_server.daemon import owner as daemon_owner

        events: list[str] = []

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        class RecordingChannel:
            def close(self) -> None:
                events.append("closed")

        def reject(*_args: object, **_kwargs: object) -> daemon_config.OwnerHandover:
            raise ValueError("invalid handed-over configuration")

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner, "_claim_startup_channel", lambda: RecordingChannel()
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
        from linkedin_mcp_server.daemon import owner as daemon_owner

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
        monkeypatch.setattr(daemon_owner, "_claim_startup_channel", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda *_args, **_kwargs: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *_args, **_kwargs: pytest.fail("pre-log failure used logging"),
        )
        if stage == "state":

            class UnresolvablePath:
                def expanduser(self) -> UnresolvablePath:
                    return self

                def resolve(self) -> Path:
                    raise OSError("profile state is unavailable")

            monkeypatch.setattr(daemon_owner, "Path", lambda _value: UnresolvablePath())
            expected = [f"diagnostic:{code}", "aborted"]
        else:

            class HeldLock:
                def release(self) -> None:
                    events.append("released")

            monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
            monkeypatch.setattr(
                daemon_owner, "_take_lock", lambda auth_root: HeldLock()
            )
            monkeypatch.setattr(
                daemon_owner,
                "_attach_daemon_log",
                lambda auth_root: (_ for _ in ()).throw(
                    OSError("daemon log is unavailable")
                ),
            )
            expected = [f"diagnostic:{code}", "aborted", "released"]

        assert daemon_owner.main([]) == 1
        assert events == expected

    @pytest.mark.parametrize(
        "protocol",
        [
            startup.PROTOCOL - 1,
            startup.PROTOCOL + 1,
            str(startup.PROTOCOL),
            float(startup.PROTOCOL),
            True,
            None,
        ],
    )
    def test_a_frontend_speaking_another_startup_protocol_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, protocol: object
    ):
        # A frontend from another build greets this owner after a package was
        # replaced under it. The owner refuses before any state is touched,
        # answers nothing a frontend could mistake for an ACK, and says only
        # that its configuration was refused. A bool is an int in Python, so
        # ``True`` is refused by type, not by value.
        from linkedin_mcp_server.daemon import owner as daemon_owner

        events: list[str] = []

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def close(self) -> None:
                pass

        frontend = _Frontend(_greeting(_config(), protocol=protocol))
        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(
            daemon_owner, "_claim_startup_channel", lambda: frontend.channel
        )
        monkeypatch.setattr(
            daemon_owner,
            "auth_root_dir",
            lambda _profile: pytest.fail("a refused handover resolved profile state"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda _auth_root: pytest.fail("a refused handover took the lock"),
        )
        try:
            assert daemon_owner.main([]) == 1
            received = frontend.received()
        finally:
            frontend.close()

        assert events == [f"diagnostic:{daemon_owner.BOOTSTRAP_CONFIGURATION}"]
        assert received == b"", "the owner answered a frontend it cannot speak to"

    def test_a_lock_failure_is_reported_before_the_daemon_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import owner as daemon_owner
        from linkedin_mcp_server.daemon.lock import DaemonLockError

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def attached(self, _log_path: Path, _nonce: str) -> None:
                events.append(f"diagnostic:{daemon_owner.BOOTSTRAP_ATTACHED}")

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
        monkeypatch.setattr(daemon_owner, "_claim_startup_channel", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda *_args, **_kwargs: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: pytest.fail("lock failure reached the daemon log"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "configure_logging",
            lambda **kwargs: pytest.fail("lock failure configured logging"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "_take_lock",
            lambda *args: (_ for _ in ()).throw(DaemonLockError("lock unusable")),
        )
        monkeypatch.setattr(
            daemon_owner.logger,
            "exception",
            lambda *args, **kwargs: pytest.fail(
                "lock failure logged before configuration"
            ),
        )

        assert daemon_owner.main([]) == 1
        assert events == [
            f"diagnostic:{daemon_owner.BOOTSTRAP_LOCK}",
            "aborted",
            "bootstrap-closed",
            "handshake-closed",
        ]

    def test_attached_bootstrap_reports_the_stage_before_the_versioned_hint(
        self, tmp_path: Path
    ):
        from linkedin_mcp_server.daemon import owner as daemon_owner

        class RecordingStream:
            def __init__(self) -> None:
                self.data = ""
                self.closed = False

            def write(self, data: str) -> int:
                self.data += data
                return len(data)

            def flush(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        stream = RecordingStream()
        log_path = (tmp_path / "daemon.log").absolute()

        daemon_owner._BootstrapDiagnostics(cast(Any, stream)).attached(log_path, _NONCE)

        assert stream.data.splitlines() == [
            f"{daemon_owner.BOOTSTRAP_PREFIX} {daemon_owner.BOOTSTRAP_ATTACHED}",
            f"{daemon_owner.BOOTSTRAP_LOG_HINT_PREFIX} "
            f"{daemon_owner.BOOTSTRAP_LOG_HINT_VERSION} {_NONCE} {log_path}",
        ]
        assert stream.closed

    def test_lock_loser_retries_without_attaching_the_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon import owner as daemon_owner

        events: list[str] = []
        config = AppConfig()
        config.browser.user_data_dir = str(tmp_path / "profile")

        class RecordingBootstrap:
            def __init__(self, _stream: object) -> None:
                pass

            def report(self, code: str) -> None:
                events.append(f"diagnostic:{code}")

            def attached(self, _log_path: Path, _nonce: str) -> None:
                pytest.fail("lock loser reported a daemon log path")

            def close(self) -> None:
                events.append("bootstrap-closed")

        class RecordingHandshake:
            def __init__(self, _stream: object, _nonce: str) -> None:
                pass

            def retry(self) -> None:
                events.append("retry")

            def close(self) -> None:
                events.append("handshake-closed")

        monkeypatch.setattr(daemon_owner, "_BootstrapDiagnostics", RecordingBootstrap)
        monkeypatch.setattr(daemon_owner, "_Handshake", RecordingHandshake)
        monkeypatch.setattr(daemon_owner, "_claim_bootstrap_stream", lambda: None)
        monkeypatch.setattr(daemon_owner, "_claim_startup_channel", lambda: None)
        monkeypatch.setattr(
            daemon_owner,
            "_read_handover",
            lambda *_args, **_kwargs: daemon_config.OwnerHandover(config, _NONCE),
        )
        monkeypatch.setattr(daemon_owner, "auth_root_dir", lambda profile: tmp_path)
        monkeypatch.setattr(
            daemon_owner,
            "_attach_daemon_log",
            lambda auth_root: pytest.fail("lock loser attached the daemon log"),
        )
        monkeypatch.setattr(
            daemon_owner,
            "configure_logging",
            lambda **kwargs: pytest.fail("lock loser configured logging"),
        )
        monkeypatch.setattr(daemon_owner, "_take_lock", lambda *args: None)

        assert daemon_owner.main([]) == 0
        assert events == ["retry", "bootstrap-closed", "handshake-closed"]

    def test_a_suspended_starter_cannot_pin_the_owner_on_config_read(self):
        import time

        from linkedin_mcp_server.daemon import owner as daemon_owner

        # The input stays open and silent, which is a starter stopped between
        # spawning this process and writing its HELLO.
        frontend = _Frontend(keep_open=True)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutError, match="did not provide its configuration"):
                daemon_owner._read_handover(frontend.channel, timeout=0.01)
            elapsed = time.monotonic() - started
        finally:
            frontend.close()

        assert elapsed < 1.0

    def test_the_owner_refuses_to_start_without_a_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # The owner reads its settings from the startup channel and must not
        # fall back to parsing its own command line: `load_config` would then
        # see the daemon's argv, and the browser it opened would be configured
        # by accident rather than by the client that asked for it.
        #
        # Exercised through `main`, which is otherwise reached only inside a
        # spawned process where coverage cannot see it. What arrives is the
        # line the old handover would have carried, which the framed channel
        # reads as a header announcing far more than a frame may hold.
        from linkedin_mcp_server.daemon import owner as daemon_owner

        frontend = _Frontend(b"   \n")
        monkeypatch.setattr(
            daemon_owner, "_claim_startup_channel", lambda: frontend.channel
        )
        try:
            assert daemon_owner.main([]) == 1
        finally:
            frontend.close()

    def test_something_that_is_not_a_configuration_is_refused(self):
        with pytest.raises(ValueError, match="not an object"):
            daemon_config.decode([1, 2, 3])

        with pytest.raises(ValueError, match="no browser section"):
            daemon_config.decode({"server": {}})
