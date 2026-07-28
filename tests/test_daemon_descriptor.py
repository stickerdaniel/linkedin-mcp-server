"""What a daemon publishes, and what a client refuses to trust.

A client that reads a descriptor is about to send a bearer token to the address
it names and then drive a logged-in LinkedIn session through it. Most of these
tests are about the cases where it must not.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest

import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_descriptor import (
    PROTOCOL_VERSION,
    SCHEMA_VERSION,
    DaemonDescriptor,
    DescriptorError,
    build,
    config_fingerprint,
    daemon_dir,
    daemon_state_root,
    descriptor_path,
    mismatched_fields,
    new_instance_id,
    new_token,
    profile_identity,
    publish,
    read,
    read_token,
    token_path,
)

posix_only = pytest.mark.skipif(
    os.name == "nt", reason="POSIX permission bits do not exist on Windows"
)
_REAL_ACCOUNT_HOME = daemon_descriptor_module._account_home


@pytest.fixture(autouse=True)
def _isolate_daemon_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Production state lives under the user's private application directory.
    # Isolate it per test rather than touching the real user directory.
    monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: tmp_path)


def _config(**browser: object) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = str(
        daemon_descriptor_module._account_home() / ".linkedin-mcp" / "profile"
    )
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


def _descriptor(
    tmp_path: Path, token: str, *, profile: Path | None = None, **overrides: object
) -> DaemonDescriptor:
    profile = profile or (tmp_path / "profile")
    descriptor = build(
        instance_id="11111111-2222-3333-4444-555555555555",
        package_version="4.19.0",
        runtime_id="macos-arm64-host",
        profile=profile,
        host="127.0.0.1",
        port=49152,
        path="/mcp",
        token=token,
        config=_config(user_data_dir=str(profile)),
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

    def test_a_number_too_long_to_parse_is_refused(self, tmp_path: Path):
        # Well under the size limit, and still unparseable: Python refuses an
        # integer with more digits than sys.int_max_str_digits allows, and does
        # it with a plain ValueError from inside the parser. Measured with five
        # thousand digits: it crossed the boundary before anything could call
        # it a bad descriptor.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        descriptor_path(tmp_path).write_text('{"port": ' + "9" * 5000 + "}")

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

    def test_an_oversized_descriptor_is_refused_as_oversized(self, tmp_path: Path):
        # The read is bounded, so without a size check the caller would get a
        # fragment, and a fragment of JSON reads as malformed. That sends
        # whoever reads the message looking for a corrupt descriptor rather
        # than for whatever wrote something this size.
        descriptor_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        descriptor_path(tmp_path).write_text(json.dumps({"pad": "x" * 200_000}))

        with pytest.raises(DescriptorError, match="larger than anything"):
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


class TestStateLocation:
    def test_state_is_outside_the_configured_auth_root(self, tmp_path: Path):
        # The auth root can be /tmp, a home directory, or another shared parent.
        # Daemon state must not change it or trust entries planted inside it.
        auth_root = tmp_path / "shared-parent"

        assert daemon_dir(auth_root).parent == daemon_state_root()
        assert auth_root not in daemon_dir(auth_root).parents

    def test_equivalent_auth_roots_share_one_state_directory(self, tmp_path: Path):
        direct = tmp_path / "shared-parent"
        indirect = tmp_path / "sub" / ".." / "shared-parent"

        assert daemon_dir(direct) == daemon_dir(indirect)

    def test_different_auth_roots_have_different_state_directories(
        self, tmp_path: Path
    ):
        one = tmp_path / "one"
        two = tmp_path / "two"
        one.mkdir()
        two.mkdir()

        assert daemon_dir(one) != daemon_dir(two)

    def test_case_aliases_share_state_on_an_insensitive_volume(self, tmp_path: Path):
        mixed = tmp_path / "MixedCase"
        lower = tmp_path / "mixedcase"
        mixed.mkdir()
        if not lower.exists():
            pytest.skip("the test volume is case-sensitive")

        assert mixed.samefile(lower)
        assert daemon_dir(mixed) == daemon_dir(lower)

    def test_process_home_overrides_do_not_move_daemon_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Launchers and services can override these per process. Election scope
        # follows the OS account, so the same absolute profile cannot split.
        monkeypatch.setattr(
            daemon_descriptor_module, "_account_home", _REAL_ACCOUNT_HOME
        )
        auth_root = tmp_path / "shared-parent"
        auth_root.mkdir()
        before = daemon_dir(auth_root)

        monkeypatch.setenv("HOME", str(tmp_path / "other-home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "other-profile"))

        assert daemon_dir(auth_root) == before

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="Linux filesystems accept non-UTF-8 byte names through surrogateescape",
    )
    def test_a_non_utf8_auth_root_spelling_can_be_keyed(self, tmp_path: Path):
        raw = os.fsencode(tmp_path) + b"/missing-\xff"
        path = Path(os.fsdecode(raw))

        assert daemon_dir(path).parent == daemon_state_root()
        assert path.is_dir()

    @pytest.mark.skipif(
        os.name == "nt", reason="creating directory symlinks needs extra privileges"
    )
    @pytest.mark.parametrize("depth", ["home", "application", "daemon"])
    def test_state_reached_through_a_link_resolves_to_one_place(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, depth: str
    ):
        # A symlinked home is an ordinary POSIX layout, so it has to work rather
        # than be refused. What matters is that two routes to it agree, which is
        # what resolving gives: the lock is addressed by path, and two spellings
        # would otherwise be two locks.
        real = tmp_path / "real"
        real.mkdir()
        home = tmp_path / "home"
        if depth == "home":
            home.symlink_to(real, target_is_directory=True)
        else:
            home.mkdir()
            application = home / ".mcp-server-linkedin"
            if depth == "application":
                application.symlink_to(real, target_is_directory=True)
            else:
                application.mkdir()
                (application / "daemon").symlink_to(real, target_is_directory=True)
        monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)

        root = daemon_state_root()

        assert not root.is_symlink()
        assert root == root.resolve()
        assert real in (root, *root.parents)

    def test_a_home_that_is_not_there_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        # Creating it would shadow a home that is merely not mounted yet, and
        # the daemon that did so would key its state under that empty directory
        # while everything started after the mount keys it under the real one.
        missing = tmp_path / "not-mounted" / "user"
        monkeypatch.setattr(
            daemon_descriptor_module, "_account_home", _REAL_ACCOUNT_HOME
        )
        monkeypatch.setattr(os, "getuid", lambda: 424242, raising=False)

        import pwd

        entry = pwd.struct_passwd(
            ("nobody", "x", 424242, 424242, "", str(missing), "/usr/bin/false")
        )
        monkeypatch.setattr(pwd, "getpwuid", lambda _uid: entry)

        with pytest.raises(DescriptorError, match="does not exist"):
            daemon_state_root()
        assert not missing.exists()

    def test_an_account_without_an_absolute_home_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        # Path("") is ".", so an empty entry would key state under whichever
        # directory the process happened to start in and split one account's
        # election between them.
        monkeypatch.setattr(
            daemon_descriptor_module, "_account_home", _REAL_ACCOUNT_HOME
        )
        monkeypatch.setattr(os, "getuid", lambda: 424242, raising=False)

        import pwd

        entry = pwd.struct_passwd(
            ("nobody", "x", 424242, 424242, "", "", "/usr/bin/false")
        )
        monkeypatch.setattr(pwd, "getpwuid", lambda _uid: entry)

        with pytest.raises(DescriptorError, match="absolute home directory"):
            daemon_state_root()

    def test_a_filesystem_without_stable_ids_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ):
        auth_root = tmp_path / "shared-parent"
        auth_root.mkdir()
        real_stat = os.stat

        def stat_without_inode(path, *args, **kwargs):
            result = real_stat(path, *args, **kwargs)
            if Path(path) == auth_root:
                fields = list(result)
                fields[1] = 0
                return os.stat_result(fields)
            return result

        monkeypatch.setattr(os, "stat", stat_without_inode)

        with pytest.raises(DescriptorError, match="stable identity"):
            daemon_dir(auth_root)


class TestEndpointSpelling:
    @pytest.mark.parametrize(
        "host,path",
        [
            ("[::1] ", "/mcp"),
            (" 127.0.0.1 ", "/mcp"),
            ("[127.0.0.1]", "/mcp"),
            ("127.0.0.1\r", "/mcp"),
            ("127.0.0.1\x01", "/mcp"),
            ("127.0.0.1\x7f", "/mcp"),
            ("127.0.0.1\xa0", "/mcp"),
            ("127.0.0.1", "/mcp\n"),
            ("127.0.0.1", "/mcp\ta"),
            ("127.0.0.1", "/mcp\x0b"),
            ("127.0.0.1", "/mcp\udcff"),
            ("127.0.0.1.", "/mcp"),
            ("127.0.0.1..", "/mcp"),
            ("localhost..", "/mcp"),
        ],
    )
    def test_an_endpoint_that_composes_into_nothing_usable_is_refused(
        self, tmp_path: Path, host: str, path: str
    ):
        # Each field is plausible on its own. Whether they compose into an
        # address a client can reach is the question that matters, and it used
        # to be answered by the client, far from anything that could explain it
        # as a descriptor this daemon did not write. Measured: "[::1] " passed
        # as loopback because the check trims and the URL does not.
        descriptor = _descriptor(tmp_path, new_token(), host=host, path=path)

        with pytest.raises(DescriptorError):
            descriptor.check_endpoint_is_local()

    @pytest.mark.parametrize(
        "answers",
        [
            [("203.0.113.9", 49152)],
            [("127.0.0.1", 49152), ("10.0.0.5", 49152)],
        ],
    )
    def test_a_name_resolving_off_this_machine_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: list
    ):
        # is_loopback_host accepts "localhost" by name, which says nothing
        # about where the resolver on this machine sends it. Measured with the
        # resolver answering 203.0.113.9: the descriptor was accepted, and the
        # bearer token would have been posted off this machine. Every answer is
        # checked, since one of several being loopback says nothing about the
        # one a client picks.
        import socket

        descriptor = _descriptor(tmp_path, new_token(), host="localhost")
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", answer)
                for answer in answers
            ],
        )

        with pytest.raises(DescriptorError, match="other than this machine"):
            descriptor.check_endpoint_is_local()

    @pytest.mark.parametrize(
        "host", ["127.0.0.1", "::1", "[::1]", "localhost", "::ffff:127.0.0.1"]
    )
    def test_every_spelling_a_daemon_publishes_is_accepted(
        self, tmp_path: Path, host: str
    ):
        # The other half: tightening this must not refuse an endpoint the
        # daemon itself would write, which is easy to do by accident.
        import httpx

        descriptor = _descriptor(tmp_path, new_token(), host=host)

        descriptor.check_endpoint_is_local()
        httpx.URL(descriptor.url)


class TestAuthRootShape:
    def test_a_file_where_the_auth_root_belongs_is_refused(self, tmp_path: Path):
        # The check for this existed but sat after secure_mkdir, which refuses
        # a non-directory itself with a NotADirectoryError that reaches the
        # caller instead of the error this module is read through. Measured
        # before the reorder: NotADirectoryError.
        occupied = tmp_path / "auth"
        occupied.write_text("a file, not a directory")

        with pytest.raises(DescriptorError, match="not a directory"):
            daemon_dir(occupied)


class TestUnusablePaths:
    @pytest.mark.parametrize(
        "path",
        [Path("~definitely-no-such-user-xyz/auth"), Path("/tmp/auth\x00x")],
    )
    def test_a_path_that_cannot_be_resolved_is_refused(self, path: Path):
        # expanduser and resolve run before anything else and have failure
        # modes of their own: an unknown user raises RuntimeError, an embedded
        # NUL raises ValueError. Both come from type-correct configuration, so
        # they are refusals rather than defects.
        with pytest.raises(DescriptorError, match="not a usable path"):
            daemon_dir(path)

        with pytest.raises(DescriptorError, match="not a usable path"):
            profile_identity(path)


class TestDigestFields:
    @pytest.mark.parametrize("field", ["token_sha256", "config_fingerprint"])
    @pytest.mark.parametrize("value", ["é" * 64, "z" * 64, "abc", "a" * 63, "a" * 65])
    def test_a_field_that_is_not_a_digest_is_refused(
        self, tmp_path: Path, field: str, value: str
    ):
        # compare_digest refuses a string carrying anything outside ASCII and
        # raises TypeError doing so. Measured with an accented character: the
        # descriptor was accepted and the comparison failed later, outside the
        # DescriptorError every caller of this module expects.
        token = new_token()
        publish(tmp_path, _descriptor(tmp_path, token), token)
        raw = json.loads(descriptor_path(tmp_path).read_text())
        raw[field] = value
        descriptor_path(tmp_path).write_text(json.dumps(raw))

        with pytest.raises(DescriptorError, match="SHA-256 digest"):
            read(tmp_path)


class TestProfileIdentityStability:
    def test_a_missing_profile_does_not_borrow_a_siblings_identity(
        self, tmp_path: Path
    ):
        # The profile does not exist before the first login. Where a sibling
        # differs only in case, folding onto it would give the two the same
        # identity, and on a case-sensitive volume they really are two
        # directories. Measured there: the identity collided before the login
        # and then changed once the directory appeared.
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        sibling = auth_root / "Profile"
        sibling.mkdir()
        profile = auth_root / "profile"
        if profile.exists():
            pytest.skip("this volume is case-insensitive, so these are one directory")

        before = profile_identity(profile)

        assert before != profile_identity(sibling)
        profile.mkdir()
        assert profile_identity(profile) == before

    @pytest.mark.parametrize("variant", ["case", "unicode"])
    def test_an_alias_of_the_profile_name_itself_matches(
        self, tmp_path: Path, variant: str
    ):
        # The alias tests above vary a parent segment. This varies the profile's
        # own name, which is the part the identity carries as text: measured, a
        # decomposed and a composed accent named one directory and produced two
        # identities, because casefold settles case and says nothing about
        # composition.
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        if variant == "case":
            first, second = "Profile", "profile"
        else:
            first = "profile" + unicodedata.normalize("NFC", "é")
            second = "profile" + unicodedata.normalize("NFD", "é")
        (auth_root / first).mkdir()
        alias = auth_root / second
        if not alias.exists():
            pytest.skip("this volume keeps the two spellings apart")

        assert profile_identity(auth_root / first) == profile_identity(alias)

    def test_case_distinct_siblings_do_not_collide(self, tmp_path: Path):
        # On a case-insensitive volume Profile and profile name one directory,
        # which is what the fold is for. On a case-sensitive one they are two,
        # and folding without preferring an exact match gave both the same
        # identity: measured on a case-sensitive APFS volume, which would have
        # handed a client the other profile's logged-in session.
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        upper = auth_root / "Profile"
        lower = auth_root / "profile"
        upper.mkdir()
        try:
            lower.mkdir()
        except FileExistsError:
            pytest.skip("this volume is case-insensitive, so these are one directory")

        assert profile_identity(upper) != profile_identity(lower)

    def test_the_identity_survives_the_profile_being_created(self, tmp_path: Path):
        # The profile does not exist before the first login, so an identity
        # that changed when it appeared would leave a descriptor published
        # beforehand no longer matching its own profile. Measured with the auth
        # root non-empty, which is the normal case since the lease files are
        # never removed: serves() went from True to False across that login.
        auth_root = tmp_path / "auth"
        auth_root.mkdir()
        (auth_root / "profile.lock").touch()
        profile = auth_root / "profile"

        before = profile_identity(profile)
        profile.mkdir()

        assert profile_identity(profile) == before


class TestInstanceIdentity:
    def test_an_instance_id_cannot_escape_the_daemon_directory(self, tmp_path: Path):
        # The identifier becomes part of a filename, and it arrives from a file
        # rather than from this process. Unchecked, a descriptor naming ".."
        # would turn some other readable file into what this client sends to
        # the endpoint as its bearer token.
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

    def test_a_rotated_profile_is_still_served(self, tmp_path: Path):
        # Every path that establishes a new session rotates the old profile into
        # quarantine and Chromium creates a fresh directory in its place. Keyed
        # by the profile's own inode, a descriptor stopped matching its own
        # profile at the first login while its owner kept holding the lock.
        profile = tmp_path / "profile"
        profile.mkdir()
        descriptor = _descriptor(tmp_path, new_token(), profile=profile)
        before = profile.stat().st_ino

        quarantine = tmp_path / "quarantine"
        quarantine.mkdir()
        profile.rename(quarantine / "profile")
        profile.mkdir()

        assert profile.stat().st_ino != before
        assert descriptor.serves(profile)

    def test_asking_what_a_daemon_serves_creates_nothing(self, tmp_path: Path):
        # Comparison answers a question. Creating a browser profile in order to
        # explain why a client was turned away would be a surprising thing for
        # that client to find afterwards.
        descriptor = _descriptor(tmp_path, new_token())
        absent = tmp_path / "never-created"

        assert not descriptor.serves(absent)
        assert not absent.exists()

    def test_reporting_a_mismatch_creates_nothing(self, tmp_path: Path):
        absent = tmp_path / "also-never-created"

        assert mismatched_fields(_config(), _config(user_data_dir=str(absent))) == (
            "user_data_dir",
        )
        assert not absent.exists()

    def test_a_case_alias_of_the_same_profile_is_served(self, tmp_path: Path):
        profile = tmp_path / "MixedRoot" / "Profile"
        profile.mkdir(parents=True)
        alias = tmp_path / "mixedroot" / "profile"
        if not alias.exists():
            pytest.skip("the test volume is case-sensitive")
        descriptor = _descriptor(tmp_path, new_token(), profile=profile)

        assert profile.samefile(alias)
        assert descriptor.serves(alias)

    def test_a_unicode_alias_of_the_same_profile_is_served(self, tmp_path: Path):
        composed = unicodedata.normalize("NFC", "é")
        decomposed = unicodedata.normalize("NFD", "é")
        profile = tmp_path / composed / "profile"
        profile.mkdir(parents=True)
        alias = tmp_path / decomposed / "profile"
        if not alias.exists():
            pytest.skip("the test volume preserves distinct Unicode spellings")
        descriptor = _descriptor(tmp_path, new_token(), profile=profile)

        assert profile.samefile(alias)
        assert descriptor.serves(alias)


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

    def test_case_aliases_of_the_same_profile_match(self, tmp_path: Path):
        profile = tmp_path / "MixedRoot" / "Profile"
        profile.mkdir(parents=True)
        alias = tmp_path / "mixedroot" / "profile"
        if not alias.exists():
            pytest.skip("the test volume is case-sensitive")
        token = new_token()

        assert config_fingerprint(
            _config(user_data_dir=str(profile)), key=token
        ) == config_fingerprint(_config(user_data_dir=str(alias)), key=token)

    def test_unicode_aliases_of_the_same_profile_match(self, tmp_path: Path):
        composed = unicodedata.normalize("NFC", "é")
        decomposed = unicodedata.normalize("NFD", "é")
        profile = tmp_path / composed / "profile"
        profile.mkdir(parents=True)
        alias = tmp_path / decomposed / "profile"
        if not alias.exists():
            pytest.skip("the test volume preserves distinct Unicode spellings")
        token = new_token()

        assert config_fingerprint(
            _config(user_data_dir=str(profile)), key=token
        ) == config_fingerprint(_config(user_data_dir=str(alias)), key=token)

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

    def test_default_auto_import_matches_explicit_enabled(self):
        # None is the enabled default, so spelling that default as True must not
        # stop an otherwise compatible client from attaching.
        token = new_token()
        default = _config()
        enabled = _config(auto_import_from_browser=True)

        assert config_fingerprint(default, key=token) == config_fingerprint(
            enabled, key=token
        )
        assert mismatched_fields(default, enabled) == ()

    def test_disabled_auto_import_remains_a_difference(self):
        token = new_token()
        default = _config()
        disabled = _config(auto_import_from_browser=False)

        assert config_fingerprint(default, key=token) != config_fingerprint(
            disabled, key=token
        )
        assert mismatched_fields(default, disabled) == ("auto_import_from_browser",)


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
