"""When a client attaches to a running daemon, and when it must not.

Attaching means sending a bearer token to an address read from a file and then
driving a logged-in LinkedIn session through whatever answers. Most of these
tests pin a case where that must not happen; the rest pin the one window where
refusing is just as wrong as attaching.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import linkedin_mcp_server.daemon as daemon_module
import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import (
    OwnerState,
    daemon_would_be_used,
    look_up_owner,
)
from linkedin_mcp_server.daemon_descriptor import (
    build,
    descriptor_path,
    new_instance_id,
    new_token,
    publish,
    read,
    token_path,
)
from linkedin_mcp_server.daemon_lock import DaemonLock

_RUNTIME = "macos-arm64-host"


@pytest.fixture(autouse=True)
def _isolate_daemon_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # Production state lives under the user's private application directory.
    monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: tmp_path)
    monkeypatch.setattr(daemon_module, "get_runtime_id", lambda: _RUNTIME)


def _config(profile: Path, **browser: object) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    for name, value in browser.items():
        setattr(config.browser, name, value)
    return config


def _publish_owner(
    auth_root: Path,
    profile: Path,
    *,
    config: AppConfig | None = None,
    runtime_id: str = _RUNTIME,
    host: str = "127.0.0.1",
) -> str:
    """Publish a descriptor as a live owner would, and return its token."""
    profile.mkdir(parents=True, exist_ok=True)
    token = new_token()
    publish(
        auth_root,
        build(
            instance_id=new_instance_id(),
            package_version="4.20.0",
            runtime_id=runtime_id,
            profile=profile,
            host=host,
            port=49152,
            path="/mcp",
            token=token,
            config=config or _config(profile),
            log_path=auth_root / "daemon.log",
        ),
        token,
    )
    return token


class TestAttaching:
    def test_a_matching_owner_is_attached_to(self, tmp_path: Path):
        profile = tmp_path / "profile"
        token = _publish_owner(tmp_path, profile)

        attachment = look_up_owner(tmp_path, profile, _config(profile)).attachment

        assert attachment is not None
        assert attachment.token == token
        assert attachment.descriptor.url == "http://127.0.0.1:49152/mcp"

    def test_no_daemon_is_not_an_error(self, tmp_path: Path):
        # The ordinary first-start case. Nothing published, nobody to talk to.
        profile = tmp_path / "profile"
        profile.mkdir()

        assert look_up_owner(tmp_path, profile, _config(profile)).attachment is None

    def test_the_token_stays_out_of_the_repr(self, tmp_path: Path):
        # This is a bearer token for a server driving a logged-in LinkedIn
        # session. Surrounding code logs whole objects at DEBUG and users paste
        # those logs into issue reports, so a default dataclass repr is how the
        # credential leaves the machine.
        profile = tmp_path / "profile"
        token = _publish_owner(tmp_path, profile)

        lookup = look_up_owner(tmp_path, profile, _config(profile))

        assert lookup.attachment is not None
        assert lookup.attachment.token == token
        assert token not in repr(lookup.attachment)
        assert token not in repr(lookup)


class TestRefusing:
    def test_a_daemon_from_another_runtime_is_refused(self, tmp_path: Path):
        # A host and a container share the mounted auth root but not their
        # filesystems. Attaching across that boundary hands back a browser
        # running against a profile in a different namespace.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile, runtime_id="docker-abc123")

        assert look_up_owner(tmp_path, profile, _config(profile)).attachment is None

    def test_a_daemon_serving_another_profile_is_refused(self, tmp_path: Path):
        # The lock is per auth root, but a browser opens a profile. Sibling
        # profiles elect one owner between them, so an owner can exist that is
        # not this client's owner.
        theirs = tmp_path / "their-profile"
        ours = tmp_path / "our-profile"
        ours.mkdir()

        # Published with our configuration, not theirs. Otherwise the profile
        # path reaches the fingerprint too and that check does the refusing,
        # leaving this one passing whether or not the profile is compared.
        # Verified by removing the check: the test still passed.
        _publish_owner(tmp_path, theirs, config=_config(ours))

        assert look_up_owner(tmp_path, ours, _config(ours)).attachment is None

    def test_a_daemon_with_a_different_configuration_is_refused(self, tmp_path: Path):
        # Attaching anyway would silently give the client a browser configured
        # differently from the one it asked for — a different proxy, in this
        # case, which is the difference between traffic leaving the machine one
        # way or another.
        profile = tmp_path / "profile"
        _publish_owner(
            tmp_path,
            profile,
            config=_config(profile, proxy_server="http://proxy.example:8080"),
        )

        assert look_up_owner(tmp_path, profile, _config(profile)).attachment is None

    def test_an_off_machine_endpoint_is_refused_before_the_token_is_sent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # The descriptor is a file, so its host is whatever was last written
        # there. Posting a bearer token to it unchecked would turn a corrupted
        # file into a way to hand the session to another machine.
        #
        # The refusal happens while parsing, in `from_mapping`, so it is in
        # force before anything here can act on the host. This test pins the
        # behaviour at the boundary a client actually calls, so that moving the
        # check would surface here rather than silently going missing.
        profile = tmp_path / "profile"
        token = _publish_owner(tmp_path, profile, host="198.51.100.7")

        with caplog.at_level("DEBUG", logger="linkedin_mcp_server.daemon"):
            assert look_up_owner(tmp_path, profile, _config(profile)).attachment is None

        # Refused for the right reason, and it says so: a silent None here
        # reads like an ordinary first start rather than a descriptor pointing
        # off the machine. The address is DEBUG-grade, because a reason can
        # quote a path or host out of the descriptor.
        assert "198.51.100.7" in caplog.text
        # And the token itself never appears in what we log about the refusal.
        assert token not in caplog.text

    def test_a_descriptor_with_no_token_beside_it_is_untrusted(self, tmp_path: Path):
        # The pair can come apart: a half-finished publish, or a token file
        # removed under a descriptor that stays. There is nothing to
        # authenticate with, and inventing a fallback would mean talking to a
        # daemon we cannot prove is ours.
        #
        # Two independent refusals cover this, which is why the test asserts
        # the outcome rather than one mechanism: the token read treats absence
        # as an error, and the digest comparison that follows would reject a
        # missing token anyway. Verified by disabling the first — the second
        # still refuses.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile)
        published = read(tmp_path)
        assert published is not None
        token_path(tmp_path, published.instance_id).unlink()

        lookup = look_up_owner(tmp_path, profile, _config(profile))

        assert lookup.state is OwnerState.UNTRUSTED
        assert lookup.attachment is None

    def test_an_unreadable_descriptor_is_not_read_as_absence(self, tmp_path: Path):
        # This is the dangerous confusion. A corrupt descriptor beside a held
        # lock means a live daemon this client cannot talk to. Returning None
        # says "attach to nobody", which is correct; what must never happen is
        # deleting it or concluding the profile is free.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile)
        descriptor_path(tmp_path).write_text("{not json", encoding="utf-8")

        assert look_up_owner(tmp_path, profile, _config(profile)).attachment is None
        assert descriptor_path(tmp_path).exists()


class TestWaitingForAStartingOwner:
    """The window between a lock being taken and a descriptor being published.

    An owner binds and starts serving before it publishes, so a client that
    arrives in between sees no descriptor and also cannot take the lock. That is
    the normal two-client startup, and treating it as "no daemon" would leave
    this process driving its own browser against the same profile for its whole
    life — the per-call handoff the daemon exists to remove.
    """

    def test_an_owner_that_publishes_late_is_still_found(self, tmp_path: Path):
        profile = tmp_path / "profile"
        profile.mkdir()
        published = threading.Event()

        def publish_after_a_moment() -> None:
            time.sleep(0.3)
            _publish_owner(tmp_path, profile)
            published.set()

        starter = threading.Thread(target=publish_after_a_moment)
        starter.start()
        try:
            lookup = look_up_owner(
                tmp_path, profile, _config(profile), wait_seconds=5.0
            )
        finally:
            starter.join()

        assert published.is_set()
        assert lookup.attachment is not None

    def test_waiting_gives_up_rather_than_hanging(self, tmp_path: Path):
        # Nothing ever publishes. The caller has to get an answer, because a
        # client blocked here is a client whose first tool call never returns.
        profile = tmp_path / "profile"
        profile.mkdir()

        started = time.monotonic()
        lookup = look_up_owner(tmp_path, profile, _config(profile), wait_seconds=0.3)

        assert lookup.attachment is None
        assert time.monotonic() - started >= 0.3

    @pytest.mark.parametrize("budget", [float("nan"), float("inf")])
    def test_a_wait_that_never_ends_is_refused(self, tmp_path: Path, budget: float):
        # `monotonic() >= nan` is false forever and so is `>= inf`, so either
        # one turns the poll into a process that never finishes starting.
        # Refused rather than clamped: an unbounded wait is a plausible thing
        # to mean and a ruinous thing to grant.
        profile = tmp_path / "profile"
        profile.mkdir()

        with pytest.raises(ValueError):
            look_up_owner(tmp_path, profile, _config(profile), wait_seconds=budget)

    @pytest.mark.parametrize("budget", [0.001, 0.01])
    def test_a_small_budget_is_not_rounded_up_to_a_poll(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: float
    ):
        # A flat poll interval turned a 1 ms budget into 101 ms: a caller who
        # asked to stay near fail-fast paid a hundredfold.
        #
        # Asserts what the code *asks for*, not what the clock shows. An upper
        # bound on elapsed time fails on a descheduled runner even when the
        # request was correct — reproduced: a 0.8 ms sleep request measured
        # 66 ms of wall time. That is a test failing at the machine rather than
        # at the code, and in CI it would be indistinguishable from a real bug.
        profile = tmp_path / "profile"
        profile.mkdir()

        requested: list[float] = []
        real_sleep = time.sleep

        def record(duration: float) -> None:
            requested.append(duration)
            real_sleep(duration)

        monkeypatch.setattr(daemon_module.time, "sleep", record)

        look_up_owner(tmp_path, profile, _config(profile), wait_seconds=budget)

        # No lower bound on elapsed time and no requirement that a sleep
        # happened at all: on a loaded machine the deadline can pass during the
        # first read, and returning immediately is then correct. Asserting
        # otherwise made this fail for the machine's reasons rather than the
        # code's, which is the same trap the upper bound fell into.
        assert all(duration <= budget for duration in requested)

    def test_a_negative_wait_is_simply_no_wait(self, tmp_path: Path):
        # Unlike a non-finite budget, this one has an obvious reading.
        profile = tmp_path / "profile"
        profile.mkdir()

        started = time.monotonic()
        lookup = look_up_owner(tmp_path, profile, _config(profile), wait_seconds=-5.0)

        assert lookup.state is OwnerState.ABSENT
        assert time.monotonic() - started < 0.2

    def test_not_waiting_is_the_default(self, tmp_path: Path):
        # Callers that only want to know the current state must not pay for a
        # wait they did not ask for.
        profile = tmp_path / "profile"
        profile.mkdir()

        started = time.monotonic()
        look_up_owner(tmp_path, profile, _config(profile)).attachment

        assert time.monotonic() - started < 0.2


class TestTellingTheRefusalsApart:
    """Why one verdict is not enough.

    "No attachment" covers a first start, an owner still binding, an owner
    running for someone else, and a descriptor that cannot be trusted. Only the
    first of those is licence to open a browser on the profile, so collapsing
    them is how a second Chromium ends up on a live session.
    """

    def test_nothing_published_is_worth_an_election_attempt(self, tmp_path: Path):
        profile = tmp_path / "profile"
        profile.mkdir()

        lookup = look_up_owner(tmp_path, profile, _config(profile))

        assert lookup.state is OwnerState.ABSENT
        assert not lookup.worth_connecting

    def test_an_incompatible_descriptor_does_not_block_an_attempt_either(
        self, tmp_path: Path
    ):
        # A valid descriptor is no more proof of a living owner than a corrupt
        # one. An owner for a sibling profile that crashed leaves exactly this
        # behind with the lock free, so refusing here strands the profile for
        # good — nothing would ever clean the file up.
        theirs = tmp_path / "their-profile"
        ours = tmp_path / "our-profile"
        ours.mkdir()
        _publish_owner(tmp_path, theirs, config=_config(ours))

        lookup = look_up_owner(tmp_path, ours, _config(ours))
        contender = DaemonLock(tmp_path)
        try:
            lock_is_free = contender.try_acquire()
        finally:
            contender.release()

        assert lookup.state is OwnerState.INCOMPATIBLE
        assert lookup.attachment is None
        assert lock_is_free, "a crashed owner leaves the descriptor, not the lock"
        assert not lookup.worth_connecting

    def test_an_untrusted_descriptor_is_kept_but_does_not_block_an_attempt(
        self, tmp_path: Path
    ):
        # Two things that look alike and are not. The file must survive — beside
        # a held lock it belongs to a live daemon. But a crashed owner leaves
        # exactly this file behind with the lock free, so refusing to even try
        # would strand the profile until someone deleted it by hand.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile)
        descriptor_path(tmp_path).write_text("{not json", encoding="utf-8")

        lookup = look_up_owner(tmp_path, profile, _config(profile))

        assert lookup.state is OwnerState.UNTRUSTED
        assert lookup.attachment is None
        assert descriptor_path(tmp_path).exists()
        assert not lookup.worth_connecting

    def test_the_descriptor_never_answers_who_holds_the_lock(self, tmp_path: Path):
        # The artifacts go out of step in both directions, so no reading of one
        # settles the other. Both halves reproduced here; the lock is the only
        # authority, and taking it is what asks.
        profile = tmp_path / "profile"
        profile.mkdir()

        # Held, nothing published: the ordinary startup window.
        holder = DaemonLock(tmp_path)
        assert holder.try_acquire()
        try:
            during_startup = look_up_owner(tmp_path, profile, _config(profile))
        finally:
            holder.release()
        assert during_startup.state is OwnerState.ABSENT

        # Free, a corrupt descriptor left behind by a crash.
        _publish_owner(tmp_path, profile)
        descriptor_path(tmp_path).write_text("{not json", encoding="utf-8")
        after_a_crash = look_up_owner(tmp_path, profile, _config(profile))
        contender = DaemonLock(tmp_path)
        try:
            assert contender.try_acquire(), "the lock is free despite the descriptor"
        finally:
            contender.release()
        assert after_a_crash.state is OwnerState.UNTRUSTED

    def test_a_replacement_owner_is_seen_through_a_dead_descriptor(
        self, tmp_path: Path
    ):
        # The tempting rule is that only ABSENT can become ATTACHABLE, because
        # a starting owner publishes late. It is wrong: a descriptor outlives
        # the owner that wrote it, so a fresh owner starting right now is read
        # through the dead one's file. Returning that at once answers a
        # question about a process that no longer exists.
        theirs = tmp_path / "their-profile"
        ours = tmp_path / "our-profile"
        ours.mkdir()
        _publish_owner(tmp_path, theirs, config=_config(ours))
        replaced = threading.Event()

        def replace_after_a_moment() -> None:
            time.sleep(0.3)
            _publish_owner(tmp_path, ours)
            replaced.set()

        starter = threading.Thread(target=replace_after_a_moment)
        starter.start()
        try:
            lookup = look_up_owner(tmp_path, ours, _config(ours), wait_seconds=5.0)
        finally:
            starter.join()

        assert replaced.is_set()
        assert lookup.state is OwnerState.ATTACHABLE

    def test_an_incompatible_descriptor_still_bounds_the_wait(self, tmp_path: Path):
        # Nothing replaces it, so the caller has to get its answer. A client
        # blocked here is a client whose first tool call never returns.
        theirs = tmp_path / "their-profile"
        ours = tmp_path / "our-profile"
        ours.mkdir()
        _publish_owner(tmp_path, theirs, config=_config(ours))

        started = time.monotonic()
        lookup = look_up_owner(tmp_path, ours, _config(ours), wait_seconds=0.3)

        assert lookup.state is OwnerState.INCOMPATIBLE
        assert time.monotonic() - started >= 0.3

    def test_a_matching_descriptor_from_a_dead_owner_is_still_only_a_file(
        self, tmp_path: Path
    ):
        # The state this module was most likely to over-read. An owner that
        # crashes *after* publishing leaves a descriptor and token that pass
        # every check, so the reading is ATTACHABLE while the lock is free and
        # nothing is listening. Connecting is what finds that out; a caller
        # whose connection fails has to fall through to the lock rather than
        # conclude a daemon exists.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile)

        lookup = look_up_owner(tmp_path, profile, _config(profile))
        contender = DaemonLock(tmp_path)
        try:
            lock_is_free = contender.try_acquire()
        finally:
            contender.release()

        assert lookup.state is OwnerState.ATTACHABLE
        assert lock_is_free, "the owner is dead; only its file remains"
        # Says what the file is, never that anything is running or attached.
        assert "running" not in lookup.reason
        assert "attached" not in lookup.reason

    def test_the_refusal_reason_carries_no_secret(self, tmp_path: Path):
        # The shared configuration includes a proxy password. A mismatch has to
        # say enough to act on and nothing more.
        profile = tmp_path / "profile"
        _publish_owner(
            tmp_path,
            profile,
            config=_config(
                profile,
                proxy_server="http://proxy.example:8080",
                proxy_password="hunter2-not-in-logs",
            ),
        )

        lookup = look_up_owner(tmp_path, profile, _config(profile))

        assert lookup.state is OwnerState.INCOMPATIBLE
        assert "hunter2-not-in-logs" not in lookup.reason
        assert "proxy.example" not in lookup.reason

    def test_a_path_in_a_reason_stays_out_of_the_info_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        # A reason can quote a path out of the descriptor, because an unusable
        # profile path is only actionable if you can see which one. INFO is
        # what users paste into issue reports, so the detail belongs a level
        # down and only the state stays up.
        profile = tmp_path / "profile"
        _publish_owner(tmp_path, profile)
        descriptor_path(tmp_path).write_text("{not json", encoding="utf-8")

        with caplog.at_level("INFO", logger="linkedin_mcp_server.daemon"):
            look_up_owner(tmp_path, profile, _config(profile)).attachment

        assert "untrusted" in caplog.text
        assert "not valid JSON" not in caplog.text


class TestWhetherTheDaemonAppliesAtAll:
    def test_the_daemon_is_off_by_default(self):
        # Supervision and liveness are unfinished, so nobody gets this without
        # asking for it.
        assert daemon_would_be_used(AppConfig()) is False

    def test_an_explicit_http_bind_does_not_use_a_daemon(self):
        # HTTP already serves every client from one process, so there is
        # nothing to deduplicate and a second listener would only add a hop.
        config = AppConfig()
        config.server.daemon_enabled = True
        config.server.transport = "streamable-http"

        assert daemon_would_be_used(config) is False

    def test_stdio_with_the_flag_on_uses_a_daemon(self, monkeypatch):
        config = AppConfig()
        config.server.daemon_enabled = True
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.get_runtime_id",
            lambda: "linux-amd64-host",
        )

        assert config.server.transport == "stdio"
        assert daemon_would_be_used(config) is True

    def test_a_container_refuses_the_daemon_and_says_why(self, monkeypatch, caplog):
        """An owner outliving its frontend would also outlive its X display."""
        config = AppConfig()
        config.server.daemon_enabled = True
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.get_runtime_id",
            lambda: "docker-amd64-container",
        )

        with caplog.at_level("WARNING", logger="linkedin_mcp_server.daemon"):
            applies = daemon_would_be_used(config)

        assert applies is False
        assert "DAEMON_ENABLED is ignored in a container" in caplog.text
        assert "virtual display" in caplog.text

    def test_container_http_needs_no_daemon_warning(self, monkeypatch, caplog):
        """HTTP is one shared server already, in every runtime."""
        config = AppConfig()
        config.server.daemon_enabled = True
        config.server.transport = "streamable-http"
        monkeypatch.setattr(
            "linkedin_mcp_server.daemon.get_runtime_id",
            lambda: "docker-arm64-container",
        )

        with caplog.at_level("WARNING", logger="linkedin_mcp_server.daemon"):
            applies = daemon_would_be_used(config)

        assert applies is False
        assert caplog.text == ""
