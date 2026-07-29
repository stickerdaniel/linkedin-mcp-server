"""Getting one owner started, and never two.

The properties here are about process lifetime, file descriptor inheritance and
the kernel's arbitration, so most of these spawn real processes. A test that
stayed inside one interpreter could show the code takes the branches it means to
and would not show the thing that matters: that exactly one browser-owning
process exists, and that the lock dies with it.

Three of these pin defects that were live in this code and found by running it:
a failed child read as "somebody is starting" and cost a full deadline, a
crashed owner's descriptor was handed back as attachable, and the parent's copy
of the lock outliving the owner would have wedged every recovery.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import linkedin_mcp_server.daemon as daemon_module
import linkedin_mcp_server.daemon_descriptor as daemon_descriptor_module
import linkedin_mcp_server.daemon_election as election_module
from linkedin_mcp_server import __version__
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import Attachment, OwnerState
from linkedin_mcp_server.daemon_election import (
    _Attempt,
    ElectionOutcome,
    obtain_owner,
)
from linkedin_mcp_server.daemon_descriptor import (
    build,
    new_instance_id,
    new_token,
    publish,
)
from linkedin_mcp_server.daemon_lock import DaemonLock, daemon_is_running

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME = "test-runtime"

_POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the lock is handed to the child only on POSIX"
)


@pytest.fixture(autouse=True)
def _isolate_daemon_state(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Keep in-process tests off the account's real daemon state.

    Skipped for the tests that spawn real owners. Those cannot use a redirected
    root, because the owner is started by production code and derives its own
    state from the account rather than from anything a test can hand it. They
    take the ``real_state_root`` fixture instead, which isolates by auth root
    and cleans up after itself.
    """
    if "real_state_root" in request.fixturenames:
        return
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(daemon_descriptor_module, "_account_home", lambda: home)
    monkeypatch.setattr(daemon_module, "get_runtime_id", lambda: _RUNTIME)
    monkeypatch.setenv("HOME", str(home))


def _config(profile: Path) -> AppConfig:
    config = AppConfig()
    config.browser.user_data_dir = str(profile)
    config.server.daemon_enabled = True
    return config


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _publish_stale_owner(
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    *,
    package_version: str = __version__,
) -> str:
    """Publish a descriptor for an owner that is not running.

    Exactly what a crash after publishing leaves behind: every field is valid,
    the token matches, and nothing is listening.
    """
    token = new_token()
    descriptor = build(
        instance_id=new_instance_id(),
        package_version=package_version,
        runtime_id=_RUNTIME,
        profile=profile,
        host="127.0.0.1",
        port=49152,
        path="/mcp",
        token=token,
        config=config,
        log_path=auth_root / "daemon.log",
    )
    publish(auth_root, descriptor, token)
    return descriptor.instance_id


class TestLiveness:
    """A descriptor is a file. Only a request that is answered is a process."""

    def test_a_crashed_owners_descriptor_is_not_handed_back(self, tmp_path: Path):
        # Reproduced against a real owner before this was fixed: the process was
        # killed, and the next election read its leftovers as attachable and
        # returned the dead endpoint. Every check passes on that file, because
        # the owner wrote it while it was healthy.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: False,
        )

        assert not outcome.worth_connecting
        assert outcome.attachment_lookup.state is OwnerState.INCOMPATIBLE

    def test_an_unreachable_descriptor_is_kept_rather_than_deleted(
        self, tmp_path: Path
    ):
        # Downgrading is not the same as cleaning up. A live owner this client
        # cannot reach for a moment would be stranded by a deletion, and so
        # would every other client attached to it. The lock decides what happens
        # next, not this reading.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)
        descriptor_file = daemon_descriptor_module.descriptor_path(auth_root)

        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: False,
        )

        assert descriptor_file.exists()

    def test_a_reachable_owner_is_attached_to_without_starting_anything(
        self, tmp_path: Path
    ):
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: True,
        )

        assert outcome.worth_connecting
        assert not outcome.started_owner

    def test_a_corpse_is_not_probed_over_and_over(self, tmp_path: Path):
        # The descriptor stays on disk until a live owner overwrites it, so a
        # loop that re-read it would probe the same dead endpoint every pass.
        # Each probe is a network timeout, which is how a fail-fast path turns
        # into a stall.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config)

        probes: list[Attachment] = []

        def never_answers(attachment: Attachment) -> bool:
            probes.append(attachment)
            return False

        obtain_owner(
            auth_root, profile, config, deadline_seconds=1.0, connect=never_answers
        )

        # Once for the corpse. Anything more means the same instance was tried
        # again after being proved dead.
        instances = {attachment.descriptor.instance_id for attachment in probes}
        assert len(probes) == len(instances)


class TestFailingFast:
    def test_a_child_that_cannot_serve_does_not_cost_the_whole_deadline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Measured before this was fixed: a failed child was indistinguishable
        # from "somebody else is starting", so the caller waited out its full
        # budget for a descriptor nothing was going to write. Sixty seconds of
        # a client apparently hanging, ending in no diagnosis at all.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent

        monkeypatch.setattr(
            election_module,
            "_start_owner",
            lambda *args, **kwargs: _Attempt.FAILED,
        )

        started = time.monotonic()
        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=30,
            connect=lambda attachment: False,
        )
        elapsed = time.monotonic() - started

        assert not outcome.worth_connecting
        # Generously bounded: the point is that it returns rather than waiting
        # out the budget, not that it returns in any particular millisecond.
        assert elapsed < 5, elapsed

    def test_losing_the_lock_race_waits_instead_of_giving_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The opposite case, and the reason the two cannot share a return value.
        # Somebody else holds the lock, so an owner *is* coming: giving up here
        # would leave this client driving its own browser against the same
        # profile for its whole life, which is the per-call handoff the daemon
        # exists to remove.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        attempts = 0

        def contended(*args: object, **kwargs: object) -> _Attempt:
            nonlocal attempts
            attempts += 1
            return _Attempt.CONTENDED

        monkeypatch.setattr(election_module, "_start_owner", contended)

        obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0.3,
            connect=lambda attachment: False,
        )

        assert attempts >= 1


# Deliberately unindented, and run as it stands rather than through
# textwrap.dedent. An indented block here reads as a nicely nested string and is
# one formatter pass away from being reindented into a different program:
# `ruff format` did exactly that to an earlier version of this file and every
# process test failed with an IndentationError from the child.
_INSPECT_OWNER = """
import json
import sys
from pathlib import Path

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner
from linkedin_mcp_server.daemon_lock import DaemonLock

profile = Path(sys.argv[1])
auth_root = profile.parent
config = AppConfig()
config.browser.user_data_dir = str(profile)

outcome = obtain_owner(auth_root, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
# Asked from *this* process, which is the frontend: it must not hold the lock it
# handed over.
print(json.dumps({
    "state": outcome.attachment_lookup.state.value,
    "started": outcome.started_owner,
    "pid": attachment.descriptor.pid if attachment else None,
    "url": attachment.descriptor.url if attachment else None,
    "frontend_holds_lock": DaemonLock(auth_root).try_acquire(),
}))
"""


#: A frontend that elects an owner and then *stays alive*, which is what a real
#: stdio server does for as long as its client is connected. It reports the
#: owner's pid and then waits, so the test can kill the owner while this process
#: still exists and ask whether the lock came free.
#:
#: Without a frontend that outlives the owner, the lock always frees: the
#: operating system closes the frontend's descriptors when it exits, so a leaked
#: copy is indistinguishable from a released one. Verified by mutation — with the
#: release removed, every other test here still passed.
_LINGERING_FRONTEND = """
import json
import sys
import time
from pathlib import Path

from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_election import obtain_owner

profile = Path(sys.argv[1])
config = AppConfig()
config.browser.user_data_dir = str(profile)

outcome = obtain_owner(profile.parent, profile, config, deadline_seconds=90)
attachment = outcome.attachment_lookup.attachment
print(json.dumps({"pid": attachment.descriptor.pid if attachment else None}))
sys.stdout.flush()
# Long enough for the caller to kill the owner and watch the lock, short enough
# that a failure cannot leave this process behind.
time.sleep(30)
"""


@pytest.fixture
def real_state_root(tmp_path: Path):
    """Let the spawned processes use the account's real daemon state root.

    The unit tests above redirect that root, and the process tests cannot:
    ``_account_home`` reads the account's passwd entry and deliberately ignores
    ``HOME`` (``daemon_descriptor.py:161-190``), precisely so that a launcher
    overriding the environment for one process cannot split an account's
    election in two. The owner is started by production code, so there is
    nowhere to inject a redirection into it, and faking one would test a path
    users never take.

    So these run against the real root, keyed by a unique auth root under
    ``tmp_path``. The daemon directory is a hash of that auth root, so nothing
    here can collide with the user's own state or with a parallel test, and the
    same derivation gives cleanup an exact target.
    """
    profile = tmp_path / "auth" / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    yield profile

    # Owners are detached, so an assertion that fails before its stop() would
    # otherwise leave a server running against the developer's machine.
    directory = daemon_descriptor_module.daemon_dir(profile.parent)
    descriptor = daemon_descriptor_module.descriptor_path(profile.parent)
    try:
        published = daemon_descriptor_module.read(profile.parent)
    except Exception:
        published = None
    if published is not None:
        _stop(published.pid)
    if directory.exists():
        import shutil

        shutil.rmtree(directory, ignore_errors=True)
    assert not descriptor.exists()


def _run_frontend(profile: Path) -> dict[str, object]:
    """Elect an owner from a separate interpreter, as a real client would."""
    import json

    result = subprocess.run(
        [sys.executable, "-c", _INSPECT_OWNER, str(profile)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        cwd=_REPO_ROOT,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _alive(pid: int) -> bool:
    """Whether a process still exists. Signal 0 checks without delivering one."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _stop(pid: object) -> None:
    """Kill an owner, taking the pid as it comes out of the child's JSON.

    Untyped on purpose: every caller has just read this out of a subprocess's
    output, and threading a cast through each of them would add noise to the
    cleanup path without making anything safer. A pid that is not an integer is
    nothing to kill.
    """
    if not isinstance(pid, int):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


@pytest.mark.slow
class TestRealOwner:
    """The whole thing, with a real detached process on the other end.

    Slow, and worth it: everything these assert is invisible to a test that
    stays in one interpreter. Each spawn starts the full server graph.
    """

    def test_an_owner_is_started_and_answers(self, real_state_root: Path):
        profile = real_state_root

        result = _run_frontend(profile)
        try:
            assert result["state"] == OwnerState.ATTACHABLE.value, result
            assert result["started"] is True
            assert result["pid"] and result["pid"] != os.getpid()
        finally:
            _stop(result.get("pid"))

    def test_the_frontend_lets_go_of_the_lock_it_handed_over(
        self, real_state_root: Path
    ):
        # The load-bearing one. Both descriptors refer to one locked open file
        # description, so a frontend that kept its copy would keep the daemon
        # lock alive after the owner died — and every recovery afterwards would
        # be locked out by a process that is not the owner and does not know it
        # holds anything.
        profile = real_state_root

        result = _run_frontend(profile)
        try:
            # Asked inside the frontend, before it exited: the lock is held by
            # the owner, and the frontend could not take it.
            assert result["frontend_holds_lock"] is False, result
        finally:
            _stop(result.get("pid"))

    def test_the_lock_frees_while_the_frontend_is_still_running(
        self, real_state_root: Path
    ):
        # The one that actually proves the release, and the reason it has to be
        # written this way. Both descriptors refer to one locked open file
        # description, so the lock lives as long as either is open. Every other
        # test here lets the frontend exit first, which closes its copy whether
        # the code released it or not — verified by mutation: with the release
        # removed, all of them still passed and this one fails.
        #
        # A frontend that leaked its copy would keep the daemon lock held after
        # the owner died, so no replacement could ever be elected while that
        # client stayed connected. That is a wedge with no visible cause.
        import json

        profile = real_state_root
        auth_root = profile.parent

        frontend = subprocess.Popen(
            [sys.executable, "-c", _LINGERING_FRONTEND, str(profile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
        )
        try:
            assert frontend.stdout is not None
            owner_pid = json.loads(frontend.stdout.readline())["pid"]
            assert isinstance(owner_pid, int), "no owner was elected"
            assert frontend.poll() is None, "the frontend exited too early"

            _stop(owner_pid)

            freed = False
            for _ in range(500):
                time.sleep(0.01)
                probe = DaemonLock(auth_root)
                if probe.try_acquire():
                    probe.release()
                    freed = True
                    break
            assert freed, (
                "the daemon lock is still held after the owner died, so the "
                "frontend kept its copy of it"
            )
        finally:
            frontend.kill()
            frontend.wait(timeout=30)

    def test_the_lock_frees_when_the_owner_dies(self, real_state_root: Path):
        # The other half of the same property. The frontend has exited by now,
        # so if it had leaked its copy the lock would still be held here even
        # though the owner is gone. Measured at 10 ms on this tree.
        profile = real_state_root
        auth_root = profile.parent

        result = _run_frontend(profile)
        pid = result["pid"]
        assert isinstance(pid, int)
        assert daemon_is_running(auth_root)

        _stop(pid)

        freed = False
        for _ in range(300):
            time.sleep(0.01)
            probe = DaemonLock(auth_root)
            if probe.try_acquire():
                probe.release()
                freed = True
                break
        assert freed, "the daemon lock outlived the owner that held it"

    def test_a_second_frontend_attaches_instead_of_starting_another_owner(
        self, real_state_root: Path
    ):
        # The point of the whole feature: one browser, however many clients.
        profile = real_state_root

        first = _run_frontend(profile)
        try:
            assert first["started"] is True
            second = _run_frontend(profile)

            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is False, "a second owner was started"
            assert second["pid"] == first["pid"]
        finally:
            _stop(first.get("pid"))

    def test_a_crashed_owner_is_replaced(self, real_state_root: Path):
        # The descriptor survives the process that wrote it, so the next client
        # must prove the endpoint before trusting it and elect a replacement
        # when it does not answer.
        profile = real_state_root

        first = _run_frontend(profile)
        dead = first["pid"]
        assert isinstance(dead, int)
        _stop(dead)
        # The kernel frees the lock at death; wait for it rather than assuming.
        for _ in range(300):
            if not daemon_is_running(profile.parent):
                break
            time.sleep(0.01)

        second = _run_frontend(profile)
        try:
            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is True, "the corpse was attached to"
            assert second["pid"] != dead
        finally:
            _stop(second.get("pid"))

    @_POSIX_ONLY
    def test_the_owner_outlives_the_client_that_started_it(self, real_state_root: Path):
        # The premise of the whole feature. An owner that died with its first
        # client would give every later client a cold start plus a fresh
        # ``/feed/`` validation, which is the traffic this exists to remove.
        #
        # Killed by process *group*, not by pid, because that is how a client
        # shutdown reaches a server it spawned — and a child that merely had its
        # own pid would still be caught by it. ``start_new_session`` is what puts
        # the owner in a group of its own.
        import json

        profile = real_state_root

        frontend = subprocess.Popen(
            [sys.executable, "-c", _LINGERING_FRONTEND, str(profile)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
            cwd=_REPO_ROOT,
            # The test process needs a group of its own too, or the killpg below
            # takes pytest with it. Found the direct way.
            start_new_session=True,
        )
        owner_pid = None
        try:
            assert frontend.stdout is not None
            owner_pid = json.loads(frontend.stdout.readline())["pid"]
            assert isinstance(owner_pid, int), "no owner was elected"

            os.killpg(os.getpgid(frontend.pid), signal.SIGKILL)
            frontend.wait(timeout=30)

            # Given a moment, because a group signal is not instantaneous and a
            # check that raced it would pass for the wrong reason.
            time.sleep(1.0)
            assert _alive(owner_pid), "the owner died with the client that started it"
        finally:
            _stop(owner_pid)
            if frontend.poll() is None:  # pragma: no cover - the kill worked
                frontend.kill()
                frontend.wait(timeout=30)

    def test_an_older_owner_hands_over_to_a_newer_build(self, real_state_root: Path):
        # The turnover, end to end against a live owner rather than a mock. Both
        # halves have to hold: the old process actually exits, and a replacement
        # is elected. Getting only the first is worse than doing nothing, and it
        # is what happened here twice while this was being built — the owner
        # stood down, and the frontend then spent its whole budget re-reading
        # the descriptor it had already rejected rather than taking the lock the
        # departing owner had just freed.
        #
        # The running owner is made to *claim* an old version by rewriting the
        # published descriptor. `package_version` is covered by neither the token
        # digest nor the configuration fingerprint, so the file stays valid in
        # every other respect and the process behind it is genuinely serving.
        import json

        profile = real_state_root
        auth_root = profile.parent

        first = _run_frontend(profile)
        old_pid = first["pid"]
        assert isinstance(old_pid, int)

        descriptor_file = daemon_descriptor_module.descriptor_path(auth_root)
        published = json.loads(descriptor_file.read_text())
        published["package_version"] = "1.0.0"
        descriptor_file.write_text(json.dumps(published, indent=2, sort_keys=True))

        started = time.monotonic()
        second = _run_frontend(profile)
        elapsed = time.monotonic() - started
        try:
            assert second["state"] == OwnerState.ATTACHABLE.value, second
            assert second["started"] is True, "no replacement owner was elected"
            assert second["pid"] != old_pid, "the stale owner is still serving"

            for _ in range(500):
                if not _alive(old_pid):
                    break
                time.sleep(0.01)
            assert not _alive(old_pid), "the stale owner never stood down"

            # And it has to be quick, because this happens on the launch after
            # every upgrade and the user is waiting for their client to come up.
            # Asserted separately from correctness because the two failed
            # separately: with the post-turnover wait reinstated the outcome was
            # still right and it took a minute and a quarter instead of three
            # seconds. Measured at 2.1s here, so the bound is generous enough for
            # a loaded CI machine and far below the stall it guards against.
            assert elapsed < 30, f"the upgrade took {elapsed:.1f}s"
        finally:
            _stop(second.get("pid"))
            _stop(old_pid)

    def test_the_owner_requires_its_token(self, real_state_root: Path):
        # The endpoint is loopback, which every process on the machine can
        # reach, and a website the user merely visits can reach through their
        # own browser. The token is what stands between that and a logged-in
        # LinkedIn session.
        import asyncio

        from fastmcp import Client
        from fastmcp.client.transports import StreamableHttpTransport

        profile = real_state_root
        result = _run_frontend(profile)
        url = result["url"]
        assert isinstance(url, str)

        try:

            async def unauthenticated() -> None:
                async with Client(StreamableHttpTransport(url, auth="wrong")) as client:
                    await client.ping()

            with pytest.raises(Exception):
                asyncio.run(unauthenticated())
        finally:
            _stop(result.get("pid"))

    def test_only_the_current_generations_token_stays_on_disk(
        self, real_state_root: Path
    ):
        # `publish` writes one token file per instance and removes none, so
        # without cleanup every restart leaves another credential behind.
        profile = real_state_root
        auth_root = profile.parent

        first = _run_frontend(profile)
        _stop(first["pid"])
        for _ in range(300):
            if not daemon_is_running(auth_root):
                break
            time.sleep(0.01)
        second = _run_frontend(profile)

        try:
            directory = daemon_descriptor_module.daemon_dir(auth_root)
            tokens = sorted(p.name for p in directory.glob("token-*"))
            assert len(tokens) == 1, tokens
            assert second["state"] == OwnerState.ATTACHABLE.value
        finally:
            _stop(second.get("pid"))


class TestVersionSkew:
    """``@latest`` means two versions can be on one machine at the same time."""

    def test_an_older_owner_is_asked_to_hand_the_browser_over(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The shipped code published `package_version` and never compared it, so
        # a months-old owner would serve its own tools to every freshly updated
        # frontend indefinitely. That is the "a pinned version quietly rots"
        # failure the README warns about, except the user *did* update.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config, package_version="1.0.0")

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            # Would attach if version were not consulted, which is what makes
            # this test about the version rather than about reachability.
            connect=lambda attachment: True,
        )

        assert asked, "the stale owner was never asked to stand down"
        assert not outcome.worth_connecting

    def test_a_newer_owner_is_used_rather_than_downgraded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The other direction, and it must not be symmetric. An older frontend
        # meeting a newer owner has found something at least as new as itself;
        # restarting the shared browser to satisfy one stale client would be the
        # worse trade, and it would also thrash whenever two versions coexist.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(auth_root, profile, config, package_version="99.0.0")

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: True,
        )

        assert not asked
        assert outcome.worth_connecting

    def test_an_unrecognisable_version_is_served_rather_than_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # A local or editable install has a version neither side can order.
        # Treating that as a skew would force a restart on every single launch,
        # which is exactly where somebody is working on this code.
        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        _publish_stale_owner(
            auth_root, profile, config, package_version="not-a-version"
        )

        asked: list[object] = []
        monkeypatch.setattr(
            election_module, "_ask_to_stand_down", lambda attachment: asked.append(1)
        )

        outcome = obtain_owner(
            auth_root,
            profile,
            config,
            deadline_seconds=0,
            connect=lambda attachment: True,
        )

        assert not asked
        assert outcome.worth_connecting

    def test_a_stand_down_request_needs_the_token(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The control route is mounted outside fastmcp's authentication
        # middleware — measured on 3.4.4, an unauthenticated POST to a custom
        # route is served — so it checks the token itself. Without that, any
        # process on the machine, and any page the user's browser visits, could
        # stop the shared browser at will.
        import asyncio
        import socket

        import httpx
        import uvicorn

        from linkedin_mcp_server import config as config_module
        from linkedin_mcp_server import daemon_owner

        config = _config(_profile(tmp_path))
        # The owner installs its handed-over configuration before it serves, and
        # the lifespan depends on that: `get_config()` would otherwise parse this
        # process's command line, which under pytest means argparse exits and
        # startup fails. Found by writing this test without it.
        #
        # Set through monkeypatch rather than `set_config`, which is a module
        # global that would then leak into every test that ran afterwards.
        monkeypatch.setattr(config_module, "_config", config)

        stopped: list[str] = []
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        port = sock.getsockname()[1]
        server = daemon_owner.create_owner_server(
            config=config,
            token="the-token",
            host="127.0.0.1",
            port=port,
            stand_down=lambda: stopped.append("asked"),
        )
        assert isinstance(server, uvicorn.Server)
        url = f"http://127.0.0.1:{port}{daemon_owner.STAND_DOWN_PATH}"

        async def exercise() -> tuple[int, int]:
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                # Through the production wait, which watches the serving task
                # alongside the flag. A bare `while not server.started` loop
                # never ends when startup fails, which is how this test hung
                # before it was written this way.
                await daemon_owner._await_started(server, serving)
                async with httpx.AsyncClient() as client:
                    without = await client.post(url)
                    wrong = await client.post(
                        url, headers={"Authorization": "Bearer nope"}
                    )
                    assert not stopped, "an unauthenticated caller stopped the daemon"
                    good = await client.post(
                        url, headers={"Authorization": "Bearer the-token"}
                    )
                    assert good.status_code == 200
                return without.status_code, wrong.status_code
            finally:
                server.should_exit = True
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(serving, timeout=15)

        without_status, wrong_status = asyncio.run(exercise())

        assert without_status == 401
        assert wrong_status == 401
        assert stopped == ["asked"]


class TestPublishingLast:
    """Nothing is discoverable until it has been proved to work."""

    def test_a_descriptor_is_published_only_after_the_endpoint_answers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The ordering is the whole point of the startup handshake, and it is
        # invisible from the outside once startup has succeeded: both orders end
        # with a published descriptor and a working endpoint. What separates them
        # is the failure case — published first, a client can find and attach to
        # an endpoint that refuses its token, and the diagnosis lands in a
        # different process from the cause.
        #
        # Verified by mutation: with publish moved ahead of the probe, every
        # other test in this file still passed.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        order: list[str] = []

        async def probe(url: str, token: str) -> None:
            order.append("probe")

        def publish_spy(root: Path, descriptor: object, token: str):
            order.append("publish")
            return Path("descriptor"), Path("token")

        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor, "publish", publish_spy, raising=True
        )
        monkeypatch.setattr(
            daemon_owner, "_forget_superseded_tokens", lambda *a, **k: None
        )

        class _StoppedServer:
            """Serves nothing and stops at once, so only the order is observed."""

            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )
        monkeypatch.setattr(
            daemon_owner, "create_owner_server", lambda **kwargs: _StoppedServer()
        )

        handshake = daemon_owner._Handshake(None)
        asyncio.run(
            daemon_owner._serve(
                lock=DaemonLock(auth_root),
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=auth_root / "daemon.log",
                ready=handshake,
            )
        )

        assert order == ["probe", "publish"], order

    def test_ready_is_signalled_only_after_the_descriptor_is_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The frontend treats "ready" as permission to go and read the
        # descriptor, so signalling before writing it turns a rare scheduling
        # order into a client that looks for a file that is not there yet and
        # elects a second owner. Verified by mutation: with the signal moved
        # ahead of the publish, nothing else in this suite noticed.
        import asyncio

        from linkedin_mcp_server import daemon_owner

        profile = _profile(tmp_path)
        config = _config(profile)
        auth_root = profile.parent
        order: list[str] = []

        class _Recording:
            started = True
            should_exit = False

            async def serve(self, sockets: object = None) -> None:
                return None

        class _RecordingHandshake:
            def succeed(self) -> None:
                order.append("ready")

            def fail(self) -> None:  # pragma: no cover - not reached here
                order.append("failed")

            def close(self) -> None:
                return None

        async def probe(url: str, token: str) -> None:
            return None

        monkeypatch.setattr(daemon_owner, "_probe", probe)
        monkeypatch.setattr(
            daemon_owner.daemon_descriptor,
            "publish",
            lambda *a, **k: (order.append("publish"), (Path("d"), Path("t")))[1],
        )
        monkeypatch.setattr(
            daemon_owner, "_forget_superseded_tokens", lambda *a, **k: None
        )
        monkeypatch.setattr(
            daemon_owner, "_bind_loopback", lambda: _FakeSocket(("127.0.0.1", 49152))
        )
        monkeypatch.setattr(
            daemon_owner, "create_owner_server", lambda **kwargs: _Recording()
        )

        asyncio.run(
            daemon_owner._serve(
                lock=DaemonLock(auth_root),
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=auth_root / "daemon.log",
                ready=_RecordingHandshake(),
            )
        )

        assert order == ["publish", "ready"], order


class _FakeSocket:
    """Stands in for the bound listening socket, which nothing here serves."""

    def __init__(self, address: tuple[str, int]) -> None:
        self._address = address

    def getsockname(self) -> tuple[str, int]:
        return self._address

    def close(self) -> None:
        return None


class TestOutcome:
    def test_the_outcome_reports_what_the_lookup_says(self, tmp_path: Path):
        # A thin wrapper, pinned because callers branch on it and the two
        # answers must not be able to disagree.
        from linkedin_mcp_server.daemon import OwnerLookup

        absent = ElectionOutcome(OwnerLookup(state=OwnerState.ABSENT))

        assert not absent.worth_connecting
