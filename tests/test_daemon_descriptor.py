"""What a daemon publishes, and what a client refuses to trust.

A client that reads a descriptor is about to send a bearer token to the address
it names and then drive a logged-in LinkedIn session through it. Most of these
tests are about the cases where it must not.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    DaemonDescriptor,
    DescriptorError,
    build,
    config_fingerprint,
    daemon_dir,
    descriptor_path,
    mismatched_fields,
    new_instance_id,
    new_token,
    publish,
    read,
    read_token,
    token_path,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits do not exist on Windows"
)


def _config(**browser: object) -> AppConfig:
    config = AppConfig()
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


def _descriptor(tmp_path: Path, token: str, **overrides: object) -> DaemonDescriptor:
    descriptor = build(
        instance_id="11111111-2222-3333-4444-555555555555",
        package_version="4.19.0",
        runtime_id="macos-arm64-host",
        profile=tmp_path / "profile",
        host="127.0.0.1",
        port=49152,
        path="/mcp",
        token=token,
        config=_config(),
        log_path=tmp_path / "daemon.log",
    )
    return replace(descriptor, **overrides) if overrides else descriptor


class TestRoundTrip:
    def test_a_published_descriptor_reads_back(self, tmp_path: Path):
        token = new_token()

        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)

        assert loaded is not None
        assert loaded.url == "http://127.0.0.1:49152/mcp"
        assert read_token(tmp_path, loaded) == token

    def test_no_descriptor_reads_as_absent_rather_than_failing(self, tmp_path: Path):
        # Absence is the ordinary first-start case, not an error.
        assert read(tmp_path) is None

    @posix_only
    def test_the_token_is_written_owner_only(self, tmp_path: Path):
        token = new_token()

        publish(tmp_path, _descriptor(tmp_path, token), token)

        file = token_path(tmp_path, "11111111-2222-3333-4444-555555555555")
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
        assert stat.S_IMODE(file.parent.stat().st_mode) == 0o700

    def test_the_token_is_not_in_the_descriptor(self, tmp_path: Path):
        # The descriptor is the readable half of the pair on purpose, so the
        # secret must not be in it. Only its digest is.
        token = new_token()

        publish(tmp_path, _descriptor(tmp_path, token), token)

        assert token not in descriptor_path(tmp_path).read_text()


class TestRefusals:
    def test_a_non_loopback_endpoint_is_refused(self, tmp_path: Path):
        # The descriptor is a file, so anything that can write to the auth root
        # could point it elsewhere. Sending the token there would hand over the
        # LinkedIn session behind it.
        token = new_token()
        descriptor = _descriptor(tmp_path, token, host="203.0.113.5")

        with pytest.raises(DescriptorError, match="not.*this machine"):
            descriptor.check_endpoint_is_local()

    def test_a_hostname_that_needs_dns_is_refused(self, tmp_path: Path):
        # DNS can point anywhere, and can change between the check and the
        # connection, so a name is never accepted as proof of locality.
        token = new_token()
        descriptor = _descriptor(tmp_path, token, host="daemon.example.test")

        with pytest.raises(DescriptorError, match="not.*this machine"):
            descriptor.check_endpoint_is_local()

    def test_corrupt_json_is_refused_rather_than_ignored(self, tmp_path: Path):
        # Refused, not treated as absent. A corrupt descriptor beside a held
        # lock means a live daemon this client cannot talk to, and treating it
        # as absent would elect a second owner against a running browser.
        publish(tmp_path, _descriptor(tmp_path, new_token()), new_token())
        descriptor_path(tmp_path).write_text("{ not json")

        with pytest.raises(DescriptorError, match="not valid JSON"):
            read(tmp_path)

    def test_a_missing_field_is_refused(self, tmp_path: Path):
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        del raw["config_fingerprint"]
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="config_fingerprint"):
            read(tmp_path)

    def test_a_port_that_is_not_a_number_is_refused(self, tmp_path: Path):
        # Read field by field rather than unpacked, so a wrong type fails here
        # with something that names the field instead of much later.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        raw["port"] = "49152"
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="port.*not a number"):
            read(tmp_path)

    def test_a_future_schema_is_refused(self, tmp_path: Path):
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        raw["schema_version"] = SCHEMA_VERSION + 1
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="version"):
            read(tmp_path)

    def test_a_token_from_another_generation_is_refused(self, tmp_path: Path):
        # The pairing check. A token whose digest does not match the descriptor
        # belongs to a different daemon, and using it would authenticate against
        # something other than what the descriptor described.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)
        assert loaded is not None
        token_path(tmp_path, loaded.instance_id).write_text(new_token())

        with pytest.raises(DescriptorError, match="different daemon generations"):
            read_token(tmp_path, loaded)

    @pytest.mark.skipif(
        os.name == "nt", reason="symlinks need a privilege this test cannot assume"
    )
    def test_a_dangling_descriptor_symlink_is_not_read_as_absence(self, tmp_path: Path):
        # Measured: a dead symlink made read() return None, so a client would
        # have taken it for a first start and elected a second owner while the
        # first was still running. Absence and untrustworthy have to stay
        # distinguishable, because only one of them means "go ahead".
        descriptor_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        descriptor_path(tmp_path).symlink_to(tmp_path / "nowhere")

        with pytest.raises(DescriptorError, match="symbolic link"):
            read(tmp_path)

    @pytest.mark.skipif(
        os.name == "nt", reason="symlinks need a privilege this test cannot assume"
    )
    def test_a_descriptor_symlink_pointing_somewhere_real_is_refused(
        self, tmp_path: Path
    ):
        # Following it would read a file this never published, chosen by
        # whoever could write the link.
        elsewhere = tmp_path / "planted.json"
        elsewhere.write_text("{}")
        descriptor_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        descriptor_path(tmp_path).symlink_to(elsewhere)

        with pytest.raises(DescriptorError, match="symbolic link"):
            read(tmp_path)

    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"), reason="named pipes are a POSIX mechanism"
    )
    def test_a_descriptor_that_is_not_a_regular_file_does_not_hang(
        self, tmp_path: Path
    ):
        # Discovery runs on every cold start, so a named pipe left at this path
        # would stall it inside open() with no timeout and no error, rather
        # than being reported as something the daemon did not write.
        descriptor_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(descriptor_path(tmp_path))

        with pytest.raises(DescriptorError, match="not something this daemon wrote"):
            read(tmp_path)

    def test_bytes_that_are_not_text_are_refused_as_such(self, tmp_path: Path):
        # A caller telling absence from untrusted state through DescriptorError
        # would otherwise meet a decoding error, which says nothing about which
        # of the two it is looking at.
        descriptor_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        descriptor_path(tmp_path).write_bytes(b"\xff\xfe")

        with pytest.raises(DescriptorError, match="not text this daemon wrote"):
            read(tmp_path)

    def test_a_token_that_is_not_text_is_refused_as_such(self, tmp_path: Path):
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)
        assert loaded is not None
        token_path(tmp_path, loaded.instance_id).write_bytes(b"\xff\xfe")

        with pytest.raises(DescriptorError, match="not text this daemon wrote"):
            read_token(tmp_path, loaded)

    def test_a_descriptor_from_another_protocol_is_refused(self, tmp_path: Path):
        # The field compatibility is meant to key on. Parsed but unenforced, a
        # client would attach to an owner whose control routes, call metadata
        # and ping contract it does not share.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        raw["protocol_version"] = PROTOCOL_VERSION + 1
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="protocol"):
            read(tmp_path)

    @pytest.mark.skipif(
        os.name == "nt", reason="symlinks need a privilege this test cannot assume"
    )
    def test_a_token_symlink_is_not_followed(self, tmp_path: Path):
        # Confining the filename to the daemon directory is not enough on its
        # own: a link sitting at that name still points wherever it likes, so
        # a planted one whose target matched the digest would have been read
        # and sent to the endpoint as this client's credential.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)
        assert loaded is not None
        elsewhere = tmp_path / "planted"
        elsewhere.write_text(token)
        path = token_path(tmp_path, loaded.instance_id)
        path.unlink()
        path.symlink_to(elsewhere)

        with pytest.raises(DescriptorError, match="could not be read"):
            read_token(tmp_path, loaded)

    @pytest.mark.skipif(
        not hasattr(os, "mkfifo"), reason="named pipes are a POSIX mechanism"
    )
    def test_a_token_that_is_not_a_regular_file_is_refused(self, tmp_path: Path):
        # A named pipe at that path would otherwise block the client inside
        # open(), before any check could run, so the read is opened
        # non-blocking and the file type is confirmed first.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)
        assert loaded is not None
        path = token_path(tmp_path, loaded.instance_id)
        path.unlink()
        os.mkfifo(path)

        with pytest.raises(DescriptorError, match="not a regular file"):
            read_token(tmp_path, loaded)

    def test_a_descriptor_with_no_token_beside_it_is_refused(self, tmp_path: Path):
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        loaded = read(tmp_path)
        assert loaded is not None
        token_path(tmp_path, loaded.instance_id).unlink()

        with pytest.raises(DescriptorError, match="no token beside it"):
            read_token(tmp_path, loaded)


class TestEndpointUrl:
    @pytest.mark.parametrize(
        "host,expected",
        [
            ("127.0.0.1", "http://127.0.0.1:49152/mcp"),
            ("localhost", "http://localhost:49152/mcp"),
            ("::1", "http://[::1]:49152/mcp"),
            ("::ffff:127.0.0.1", "http://[::ffff:127.0.0.1]:49152/mcp"),
            ("[::1]", "http://[::1]:49152/mcp"),
        ],
    )
    def test_every_accepted_host_produces_a_usable_url(
        self, tmp_path: Path, host: str, expected: str
    ):
        # An IPv6 literal that is accepted but unbracketed parses as a bad
        # port. Measured: a valid ::1 endpoint produced a URL no client could
        # use, so the daemon was unreachable through its own descriptor.
        import httpx

        descriptor = _descriptor(tmp_path, new_token(), host=host)
        descriptor.check_endpoint_is_local()

        assert descriptor.url == expected
        httpx.URL(descriptor.url)  # raises if a client could not use it


class TestInstanceIdentity:
    def test_an_instance_id_cannot_escape_the_daemon_directory(self, tmp_path: Path):
        # The identifier becomes part of a filename, and it arrives from a file
        # anything with write access to the auth root can edit. Unchecked, a
        # descriptor naming ".." would turn some other readable file into what
        # this client sends to the endpoint as its bearer token.
        with pytest.raises(DescriptorError, match="not a UUID"):
            token_path(tmp_path, "../../../../etc/hosts")

    def test_a_descriptor_with_a_forged_instance_id_is_refused(self, tmp_path: Path):
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        raw["instance_id"] = "../../secrets"
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="not a UUID"):
            read(tmp_path)

    def test_a_generated_instance_id_is_accepted(self, tmp_path: Path):
        # The identity the daemon actually publishes has to survive its own
        # validation, which is easy to break by tightening the check.
        assert token_path(tmp_path, new_instance_id()).parent == daemon_dir(tmp_path)


class TestProfileIdentity:
    def test_a_sibling_profile_is_not_served(self, tmp_path: Path):
        # Measured: auth_root_dir returns the profile's parent, so profile and
        # profile2 share one lock and therefore one election. Without an exact
        # comparison a client configured for one silently gets the other's
        # browser, complete with the wrong logged-in session.
        descriptor = _descriptor(tmp_path, new_token())

        assert descriptor.serves(tmp_path / "profile")
        assert not descriptor.serves(tmp_path / "profile2")

    def test_a_different_spelling_of_the_same_profile_is_served(self, tmp_path: Path):
        # Same directory reached by a longer route is still the same browser.
        descriptor = _descriptor(tmp_path, new_token())

        assert descriptor.serves(tmp_path / "sub" / ".." / "profile")


class TestConfigFingerprint:
    def test_identical_configuration_matches(self):
        token = new_token()

        assert config_fingerprint(_config(), key=token) == config_fingerprint(
            _config(), key=token
        )

    @pytest.mark.parametrize(
        "field,value",
        [
            ("headless", False),
            ("user_agent", "custom-agent"),
            ("viewport_width", 1920),
            ("proxy_server", "http://proxy.example:8080"),
            ("proxy_password", "hunter2"),
            ("slow_mo", 250),
        ],
    )
    def test_configuration_that_changes_the_browser_does_not_match(
        self, field: str, value: object
    ):
        # Each of these changes what the shared browser is, so a client that
        # disagrees cannot be served by that owner.
        token = new_token()

        assert config_fingerprint(_config(), key=token) != config_fingerprint(
            _config(**{field: value}), key=token
        )

    def test_configuration_that_only_affects_the_client_still_matches(self):
        # These describe how one client behaves, not what the browser is, so
        # they must not stop it attaching.
        token = new_token()

        assert config_fingerprint(_config(), key=token) == config_fingerprint(
            _config(browser_wait_seconds=10.0, browser_min_hold_seconds=5.0),
            key=token,
        )

    def test_the_fingerprint_is_keyed_to_the_daemon(self):
        # Unkeyed, the digest would sit in a readable file covering a proxy
        # password and otherwise guessable settings, which is an offline check
        # for password guesses. Keyed, only a process that already has the token
        # can compute it.
        assert config_fingerprint(_config(), key=new_token()) != config_fingerprint(
            _config(), key=new_token()
        )

    def test_the_same_profile_spelled_differently_matches(self, tmp_path: Path):
        token = new_token()
        direct = _config(user_data_dir=str(tmp_path / "profile"))
        indirect = _config(user_data_dir=str(tmp_path / "sub" / ".." / "profile"))

        assert config_fingerprint(direct, key=token) == config_fingerprint(
            indirect, key=token
        )

    def test_an_empty_proxy_password_is_not_an_absent_one(self):
        # proxy_settings compares the password against None precisely so an
        # empty one is still sent to Chromium. Measured before the fix: the two
        # produced different launch options and an identical fingerprint, so a
        # client would have been served a browser configured differently from
        # what it asked for.
        token = new_token()
        absent = _config(proxy_server="http://p.example:1", proxy_username="u")
        empty = _config(
            proxy_server="http://p.example:1", proxy_username="u", proxy_password=""
        )

        assert absent.browser.proxy_settings() != empty.browser.proxy_settings()
        assert config_fingerprint(absent, key=token) != config_fingerprint(
            empty, key=token
        )

    def test_reordered_proxy_bypass_hosts_match(self):
        # The same hosts bypass the proxy either way, so the order is not a
        # difference worth refusing over.
        token = new_token()
        one = _config(proxy_server="http://p.example:1", proxy_bypass="a.com, b.com")
        other = _config(proxy_server="http://p.example:1", proxy_bypass="b.com,a.com")

        assert config_fingerprint(one, key=token) == config_fingerprint(
            other, key=token
        )


class TestMismatchReporting:
    def test_mismatches_name_fields(self):
        differing = mismatched_fields(_config(), _config(headless=False))

        assert differing == ("headless",)

    def test_a_mismatch_report_carries_no_values(self):
        # What gets shown to a user. The values include a proxy password and the
        # path to someone's browser profile, so the report names fields only.
        differing = mismatched_fields(
            _config(proxy_password="hunter2"), _config(proxy_password="letmein")
        )

        assert differing == ("proxy_password",)
        assert "hunter2" not in str(differing)
