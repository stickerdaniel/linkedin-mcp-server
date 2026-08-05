"""What this server is allowed to move and delete, and how it proves it.

Every refusal test here works against a directory under ``tmp_path`` and asserts
that sentinel files survive. That is deliberate rather than incidental: these
tests exist to be run with the guard removed, and a test that pointed a
destructive call at ``/`` or at a real home directory would, under exactly that
mutation, do the damage it was written to prevent. The two paths nobody may
name are checked against the pure predicate instead, which moves nothing even
when it is wrong.
"""

import json
import os
import time
from pathlib import Path

import pytest

from linkedin_mcp_server.exceptions import ProfileRootRefusedError
from linkedin_mcp_server.profile_claim import (
    CLAIM_FILE,
    CLAIM_VERSION,
    claim_path,
    default_profile_dir,
    ensure_profile_claim,
    require_profile_claim,
)
from linkedin_mcp_server.session_state import (
    clear_auth_state,
    clear_runtime_profile,
    portable_cookie_path,
    reset_source_profile,
    restore_source_profile,
    rotate_source_profile,
    source_state_path,
)


#: Long enough that a second process racing into the same decision has landed,
#: short enough not to weigh on the suite. Only spent when the guard works.
_A_GENEROUS_MOMENT = 0.25

#: Every cross-process wait below is bounded by this. Measured why: an
#: unbounded ``os.read`` on a pipe a forked child never wrote to wedged the
#: whole suite at ten minutes of wall clock and nine seconds of CPU, and a run
#: that hangs says nothing about which test broke.
_PATIENCE_SECONDS = 15.0


def _await_signal(fd: int, *, what: str, seconds: float = _PATIENCE_SECONDS) -> bytes:
    """Read one byte, or fail the test rather than hang the suite."""
    os.set_blocking(fd, False)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            data = os.read(fd, 1)
        except BlockingIOError:
            time.sleep(0.01)
            continue
        if data:
            return data
        break  # every writer is gone, and none of them wrote
    raise AssertionError(f"timed out waiting for {what}")


def _wait_quietly(fd: int, seconds: float = _PATIENCE_SECONDS) -> None:
    """A forked child's side of the same wait, giving up instead of orphaning.

    A child blocked forever on a release that never comes outlives the test and
    keeps the lease it holds, which is how one broken assertion turns into every
    later test failing for an unrelated reason.
    """
    os.set_blocking(fd, False)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if os.read(fd, 1):
                return
        except BlockingIOError:
            time.sleep(0.01)
            continue
        return


def _foreign_root(tmp_path, name: str = "Documents") -> Path:
    """Somebody else's directory, with something in it worth not losing."""
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "thesis.pdf").write_text("years of work")
    (root / "photos").mkdir()
    (root / "photos" / "wedding.jpg").write_text("also years of work")
    return root


def _sentinels_survive(root: Path) -> bool:
    return (
        (root / "thesis.pdf").exists()
        and (root / "photos" / "wedding.jpg").exists()
        and (root / "thesis.pdf").read_text() == "years of work"
    )


def _seed_session(profile_dir: Path) -> None:
    """The four artifacts a committed source session consists of."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Local State").write_text('{"machine_id": "seeded"}')
    portable_cookie_path(profile_dir).write_text("[]")
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-08-05T00:00:00Z",
                "profile_path": str(profile_dir.resolve()),
                "cookies_path": str(portable_cookie_path(profile_dir)),
            }
        )
    )


def _write_marker(profile_dir: Path, payload: object) -> Path:
    marker = claim_path(profile_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return marker


class TestTheDefaultRootNeedsNoMarker:
    """The one root that is ours by definition, because nothing else can be.

    In the published image the default expands to
    ``/home/pwuser/.linkedin-mcp/profile``, and the volume mounted there was
    written on a host under a completely different path. No comparison of
    recorded paths can ever succeed across that boundary, so the default has to
    stand on its own.
    """

    def test_it_is_accepted_with_no_marker_at_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        default = default_profile_dir()

        assert require_profile_claim(default) == default
        assert not claim_path(default).exists()

    def test_a_marker_from_another_machine_does_not_stop_it(
        self, tmp_path, monkeypatch
    ):
        """The container case, which is the reason the rule exists.

        A mounted volume arrives carrying whatever the host wrote, and that path
        can never equal the container's own. Without the default rule this is a
        mismatch and the container is refused on the volume it was given.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        default = default_profile_dir()
        _write_marker(
            default,
            {
                "version": CLAIM_VERSION,
                "profile_path": "/Users/someone/.linkedin-mcp/profile",
            },
        )

        assert ensure_profile_claim(default) == default
        assert require_profile_claim(default) == default

    def test_it_leaves_that_marker_exactly_as_it_found_it(self, tmp_path, monkeypatch):
        """Rewriting it would break the operator who mounts a custom host root.

        The container would stamp its own path over the host's, and the next
        host run would read its own auth root as somebody else's and refuse.
        Since the default is recognised without a marker, there is nothing to
        write.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        default = default_profile_dir()
        marker = _write_marker(
            default,
            {"version": CLAIM_VERSION, "profile_path": "/host/custom/profile"},
        )
        before = marker.read_text()

        ensure_profile_claim(default)

        assert marker.read_text() == before

    def test_an_occupied_default_root_is_still_ours(self, tmp_path, monkeypatch):
        """The browser cache and the session both live here; neither disqualifies it."""
        monkeypatch.setenv("HOME", str(tmp_path))
        default = default_profile_dir()
        default.parent.mkdir(parents=True)
        (default.parent / "browsers").mkdir()
        (default.parent / "update-check.json").write_text("{}")

        assert ensure_profile_claim(default) == default
        assert not claim_path(default).exists()


class TestRefusingARootNobodyClaimed:
    def test_the_filesystem_root_is_refused(self):
        """Checked as a predicate, never by pointing a delete at it."""
        with pytest.raises(ProfileRootRefusedError):
            require_profile_claim(Path("/"))

    def test_the_home_directory_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ProfileRootRefusedError):
            require_profile_claim(Path("~").expanduser())

    def test_rotation_moves_nothing_out_of_a_foreign_directory(self, tmp_path):
        foreign = _foreign_root(tmp_path)

        with pytest.raises(ProfileRootRefusedError):
            rotate_source_profile(foreign / "profile")

        assert _sentinels_survive(foreign)

    def test_logout_deletes_nothing_out_of_a_foreign_directory(self, tmp_path):
        foreign = _foreign_root(tmp_path)

        with pytest.raises(ProfileRootRefusedError):
            clear_auth_state(foreign / "profile")

        assert _sentinels_survive(foreign)

    def test_a_failed_import_resets_nothing_in_a_foreign_directory(self, tmp_path):
        """The path that used to be a bare rmtree behind a private helper."""
        foreign = _foreign_root(tmp_path)
        (foreign / "profile").mkdir()
        (foreign / "profile" / "keep-me").write_text("staged")

        with pytest.raises(ProfileRootRefusedError):
            reset_source_profile(foreign / "profile")

        assert (foreign / "profile" / "keep-me").exists()
        assert _sentinels_survive(foreign)

    def test_clearing_a_derived_runtime_refuses_a_foreign_root(self, tmp_path):
        """It raises like its siblings; the teardown that must not see a raise
        catches it at the call site instead.

        Reporting False from the function itself served the teardown and quietly
        broke the other call site, which clears the derived profile *before* a
        bridge starts. Continuing past that one imports cookies over a profile
        from an earlier login generation.
        """
        foreign = _foreign_root(tmp_path)

        with pytest.raises(ProfileRootRefusedError):
            clear_runtime_profile("linux-amd64-container", foreign / "profile")

        assert _sentinels_survive(foreign)

    def test_a_relative_spelling_of_a_foreign_root_is_refused(
        self, tmp_path, monkeypatch
    ):
        """The same directory named the long way round is the same directory."""
        foreign = _foreign_root(tmp_path)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(ProfileRootRefusedError):
            rotate_source_profile(Path("Documents") / "profile")

        assert _sentinels_survive(foreign)

    def test_a_symlink_onto_a_foreign_root_is_refused(self, tmp_path):
        """The case a lexical check misses.

        ``shutil.move`` relocates the *link* while the sidecars are computed
        from the target's parent, so an unresolved path splits one session
        across two directories with no error anywhere.
        """
        foreign = _foreign_root(tmp_path)
        link = tmp_path / "innocent"
        link.symlink_to(foreign, target_is_directory=True)

        with pytest.raises(ProfileRootRefusedError):
            rotate_source_profile(link / "profile")

        assert _sentinels_survive(foreign)

    def test_a_symlinked_route_to_our_own_root_is_still_ours(self, tmp_path):
        """One directory, two spellings, and resolving is what makes them one.

        The other half of the symlink problem, and the half a refusal test
        cannot reach: expanding without resolving would read this alias as a
        different profile than the marker names and refuse a legitimate
        session. It would also hand the unresolved spelling downstream, where
        the profile is moved lexically while its cookies and metadata are
        computed from the resolved parent — one session, two roots, no error.
        """
        real = tmp_path / "real" / "profile"
        ensure_profile_claim(real)
        alias = tmp_path / "alias"
        alias.symlink_to(tmp_path / "real", target_is_directory=True)

        assert require_profile_claim(alias / "profile") == real.resolve()

    def test_an_empty_profile_beside_a_foreign_cookie_file_is_refused(self, tmp_path):
        """Emptiness of the profile says nothing about what is next to it.

        Rotation moves ``<parent>/cookies.json``, ``<parent>/source-state.json``
        and ``<parent>/runtime-profiles`` as well, so judging the profile alone
        is judging the wrong directory.
        """
        foreign = tmp_path / "someone-elses-tool"
        (foreign / "profile").mkdir(parents=True)
        (foreign / "cookies.json").write_text("their cookies")

        with pytest.raises(ProfileRootRefusedError):
            rotate_source_profile(foreign / "profile")

        assert (foreign / "cookies.json").read_text() == "their cookies"

    def test_restoring_from_a_foreign_quarantine_is_refused(
        self, isolate_profile_dir, tmp_path
    ):
        """A backup directory is read *and* written beside, so it is checked too."""
        elsewhere = (
            tmp_path / "elsewhere" / "invalid-state-2026-01-01T00-00-00Z-abcd1234"
        )
        elsewhere.mkdir(parents=True)
        (elsewhere / "cookies.json").write_text("not ours")

        with pytest.raises(ProfileRootRefusedError):
            restore_source_profile(elsewhere, isolate_profile_dir)

        assert (elsewhere / "cookies.json").exists()

    def test_a_quarantine_name_outside_our_root_is_refused(
        self, isolate_profile_dir, tmp_path
    ):
        """Right name, wrong parent."""
        stranger = tmp_path / "stranger"
        stranger.mkdir()

        with pytest.raises(ProfileRootRefusedError):
            restore_source_profile(stranger, isolate_profile_dir)

    def test_a_directory_in_our_root_with_the_wrong_name_is_refused(
        self, isolate_profile_dir
    ):
        """Right parent, wrong name: only our own quarantines are restorable."""
        intruder = isolate_profile_dir.parent / "not-a-quarantine"
        intruder.mkdir()
        (intruder / "cookies.json").write_text("someone else's")

        with pytest.raises(ProfileRootRefusedError):
            restore_source_profile(intruder, isolate_profile_dir)

        assert (intruder / "cookies.json").exists()


class TestClaimingACustomRoot:
    def test_a_missing_root_may_claim(self, tmp_path):
        target = tmp_path / "nothing-here-yet" / "profile"

        assert ensure_profile_claim(target) == target
        assert require_profile_claim(target) == target

    def test_an_empty_root_may_claim(self, tmp_path):
        target = tmp_path / "empty" / "profile"
        target.parent.mkdir()

        assert ensure_profile_claim(target) == target

    def test_the_lease_files_do_not_count_as_occupancy(self, tmp_path):
        """This server writes them while looking at a root it has not claimed yet.

        Counting them would make every fresh custom root permanently
        unclaimable, on the very run that was supposed to claim it.
        """
        target = tmp_path / "fresh" / "profile"
        target.parent.mkdir()
        (target.parent / "profile.lock").write_text("")
        (target.parent / "profile.handoff").write_text("")

        assert ensure_profile_claim(target) == target

    def test_our_own_startup_debris_does_not_count_against_it(self, tmp_path):
        """The failure that shipped past every other test in this file.

        ``main()`` configures logging before it claims, and trace capture
        defaults to ``on_error`` rather than off, so ``trace-runs`` is already
        in the auth root by the time emptiness is judged. The lease writes its
        lock just as early. Measured against the real entry point with a
        genuinely empty custom directory: refused, and told to point at an empty
        directory.

        The other tests missed it because they call this function directly, with
        none of the startup that precedes it in a real run.
        """
        target = tmp_path / "custom" / "profile"
        root = target.parent
        root.mkdir()
        (root / "trace-runs").mkdir()
        (root / "profile.lock").write_text("")
        (root / "issue-reports").mkdir()
        (root / "patchright-browsers").mkdir()
        (root / "browser-install.json").write_text("{}")

        assert ensure_profile_claim(target) == target

    def test_the_ignored_names_are_the_ones_actually_written(self):
        """Pinned to their sources, because a rename would restore the refusal.

        The list is spelled out rather than imported to avoid an import cycle,
        and a hand-copied constant is exactly the kind that drifts.
        """
        from linkedin_mcp_server import bootstrap, profile_lease
        from linkedin_mcp_server.profile_claim import (
            _IGNORED_WHEN_JUDGING_EMPTINESS as ignored,
        )

        assert profile_lease._LEASE_FILE in ignored
        assert profile_lease._HANDOFF_FILE in ignored
        assert bootstrap._BROWSER_DIR in ignored
        assert bootstrap._BROWSER_INSTALL_METADATA in ignored

    def test_an_occupied_root_is_refused_and_keeps_everything(self, tmp_path):
        foreign = _foreign_root(tmp_path)

        with pytest.raises(ProfileRootRefusedError, match="already holds files"):
            ensure_profile_claim(foreign / "profile")

        assert _sentinels_survive(foreign)
        assert not claim_path(foreign / "profile").exists()

    def test_an_empty_profile_in_an_occupied_root_never_qualifies(self, tmp_path):
        foreign = _foreign_root(tmp_path)
        (foreign / "profile").mkdir()

        with pytest.raises(ProfileRootRefusedError):
            ensure_profile_claim(foreign / "profile")

        assert _sentinels_survive(foreign)

    def test_an_existing_session_migrates_without_being_asked(self, tmp_path):
        """Months of working installation, no marker, and no reason to interrupt.

        ``write_source_state`` has always recorded the resolved profile path, so
        the proof was already on disk before this mechanism existed.
        """
        target = tmp_path / "custom" / "profile"
        _seed_session(target)

        assert ensure_profile_claim(target) == target
        assert require_profile_claim(target) == target

    def test_a_session_recorded_for_another_path_does_not_migrate(self, tmp_path):
        """A session file naming somewhere else proves nothing about here."""
        target = tmp_path / "custom" / "profile"
        target.mkdir(parents=True)
        source_state_path(target).write_text(
            json.dumps(
                {
                    "version": 1,
                    "source_runtime_id": "macos-arm64-host",
                    "login_generation": "gen-1",
                    "created_at": "2026-08-05T00:00:00Z",
                    "profile_path": "/somewhere/else/profile",
                    "cookies_path": "/somewhere/else/cookies.json",
                }
            )
        )

        with pytest.raises(ProfileRootRefusedError):
            ensure_profile_claim(target)

    def test_the_operator_may_take_an_occupied_root_on_purpose(self, tmp_path):
        """The historical Docker mount, and the root that already holds a browser."""
        foreign = _foreign_root(tmp_path)

        assert ensure_profile_claim(foreign / "profile", claim_anyway=True) == (
            foreign / "profile"
        )
        assert require_profile_claim(foreign / "profile") == foreign / "profile"
        assert _sentinels_survive(foreign)

    def test_the_operator_may_take_over_a_root_claimed_under_another_path(
        self, tmp_path
    ):
        """The case the flag was written for, and the one it could not reach.

        A tree claimed on a host under one path and later mounted somewhere else
        carries a marker that can never match. Behind the marker check the flag
        was dead code exactly here, while the refusal message recommended it.
        """
        target = tmp_path / "mounted" / "profile"
        _write_marker(
            target, {"version": CLAIM_VERSION, "profile_path": "/srv/profile"}
        )

        with pytest.raises(ProfileRootRefusedError, match="claim-profile-root"):
            ensure_profile_claim(target)

        assert ensure_profile_claim(target, claim_anyway=True) == target
        assert require_profile_claim(target) == target

    def test_the_operator_may_take_over_an_unreadable_marker(self, tmp_path):
        """Debris should not need a manual delete to get past."""
        target = tmp_path / "mounted" / "profile"
        _write_marker(target, "{corrupt")

        assert ensure_profile_claim(target, claim_anyway=True) == target
        assert require_profile_claim(target) == target

    def test_a_marker_naming_an_unusable_path_refuses_instead_of_crashing(
        self, tmp_path
    ):
        """A hand-edited marker can hold something no filesystem will parse."""
        target = tmp_path / "custom" / "profile"
        _write_marker(
            target, {"version": CLAIM_VERSION, "profile_path": "/bad\x00path"}
        )

        with pytest.raises(ProfileRootRefusedError):
            require_profile_claim(target)
        with pytest.raises(ProfileRootRefusedError):
            ensure_profile_claim(target)

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param("{not json", id="malformed"),
            pytest.param({"version": 1}, id="no-path"),
            pytest.param({"version": 1, "profile_path": ""}, id="empty-path"),
            pytest.param(
                {"version": 99, "profile_path": "/x/profile"}, id="future-version"
            ),
            pytest.param([1, 2, 3], id="not-an-object"),
        ],
    )
    def test_an_unreadable_marker_refuses_rather_than_falling_back(
        self, tmp_path, payload
    ):
        """Fail closed: an unreadable marker is not the same as no marker.

        Treating it as absent would let a corrupted byte re-open the
        empty-root path and re-claim a root whose real owner is unknown.
        """
        target = tmp_path / "custom" / "profile"
        target.parent.mkdir()
        _write_marker(target, payload)

        with pytest.raises(ProfileRootRefusedError):
            ensure_profile_claim(target)
        with pytest.raises(ProfileRootRefusedError):
            require_profile_claim(target)

    def test_a_marker_for_a_sibling_refuses_this_profile(self, tmp_path):
        """Two profiles in one auth root; the root belongs to whichever claimed it."""
        root = tmp_path / "shared"
        root.mkdir()
        assert ensure_profile_claim(root / "first") == root / "first"

        with pytest.raises(ProfileRootRefusedError, match="It belongs to"):
            ensure_profile_claim(root / "second")

    def test_the_marker_name_is_reserved_for_the_marker(self, tmp_path):
        with pytest.raises(ProfileRootRefusedError, match="reserved"):
            ensure_profile_claim(tmp_path / "root" / CLAIM_FILE)

    def test_a_derived_runtime_profile_may_not_claim(self, isolate_profile_dir):
        """The wrong-root mistake, refused rather than granted.

        ``<auth-root>/runtime-profiles/<id>/profile`` has its own nested auth
        root, which ``clear_runtime_profile`` deletes wholesale. A marker there
        would be destroyed by ordinary operation, and while it lived it would
        have protected a directory the server removes on purpose.
        """
        derived = (
            isolate_profile_dir.parent
            / "runtime-profiles"
            / "linux-amd64-container"
            / "profile"
        )

        with pytest.raises(ProfileRootRefusedError, match="derived runtime profile"):
            ensure_profile_claim(derived)

        assert not (derived.parent / CLAIM_FILE).exists()


class TestTheMarkerOutlivesEveryOperation:
    """Rotation quarantines the session and logout deletes it. Both are moments
    the *next* run needs the claim most, so the marker is outside the target
    list and outside the quarantine glob."""

    def test_rotation_leaves_it_in_place(self, isolate_profile_dir):
        _seed_session(isolate_profile_dir)
        marker = claim_path(isolate_profile_dir)
        assert marker.exists()

        backup = rotate_source_profile(isolate_profile_dir)

        assert backup is not None
        assert marker.exists()
        assert not (backup / CLAIM_FILE).exists()

    def test_restoring_leaves_it_in_place(self, isolate_profile_dir):
        _seed_session(isolate_profile_dir)
        backup = rotate_source_profile(isolate_profile_dir)
        assert backup is not None

        assert restore_source_profile(backup, isolate_profile_dir) is True
        assert claim_path(isolate_profile_dir).exists()

    def test_logout_leaves_it_in_place(self, isolate_profile_dir):
        _seed_session(isolate_profile_dir)

        assert clear_auth_state(isolate_profile_dir) is True

        assert claim_path(isolate_profile_dir).exists()
        assert require_profile_claim(isolate_profile_dir) == isolate_profile_dir

    def test_a_failed_import_leaves_it_in_place(self, isolate_profile_dir):
        _seed_session(isolate_profile_dir)

        reset_source_profile(isolate_profile_dir)

        assert not isolate_profile_dir.exists()
        assert not portable_cookie_path(isolate_profile_dir).exists()
        assert claim_path(isolate_profile_dir).exists()


#: These need a second *process* rather than a second thread: the lease and the
#: claim lock are reference-counted per process, so two threads would both be
#: granted and prove nothing. ``fork`` is the cheapest way to get one, and it
#: does not exist on Windows — skipped there rather than left to raise
#: ``AttributeError``, which is what the CI platform job would report if its
#: file list ever grew to include this one.
needs_fork = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="fork does not exist on Windows"
)


@needs_fork
class TestTwoProcessesMeetingOneRoot:
    def test_exactly_one_of_two_siblings_wins(self, tmp_path, monkeypatch):
        """Held inside the decision, not merely started together.

        A barrier before the call is not enough, and the first version of this
        test proved it: with the lease removed the two processes still ran one
        after the other, the first wrote its marker, the second read it and
        refused, and the test passed while protecting nothing. Two processes
        starting at the same moment do not stay together across a filesystem
        read.

        So the barrier sits where the race actually is. Each child blocks inside
        ``_is_free``, which is the last thing consulted before the write, and is
        released only once a child has reached it. Under the lease exactly one
        child ever gets there, because the other is still waiting to acquire;
        without it both do, both find the root free, and both write.
        """
        import linkedin_mcp_server.profile_claim as claim_module

        root = tmp_path / "shared"
        root.mkdir()
        inside_read, inside_write = os.pipe()
        release_read, release_write = os.pipe()

        real_is_free = claim_module._is_free

        def held_open(profile_dir: Path) -> bool:
            answer = real_is_free(profile_dir)
            os.write(inside_write, b"1")
            _wait_quietly(release_read)
            return answer

        # Installed before the fork so both children inherit it. Harmless in
        # this process, which never reaches a claim decision from here.
        monkeypatch.setattr(claim_module, "_is_free", held_open)

        children = []
        for name in ("first", "second"):
            pid = os.fork()
            if pid == 0:
                status = 1
                try:
                    ensure_profile_claim(root / name)
                    status = 0
                except ProfileRootRefusedError:
                    status = 3
                except BaseException:
                    status = 4
                finally:
                    os._exit(status)
            children.append(pid)

        # Assert the exclusion directly rather than inferring it from the exit
        # codes. Inference is not enough and was measured not to be: unguarded,
        # the loser still refuses whenever it happens to read the marker after
        # the winner wrote it, so the exit codes came out correct in a full-file
        # run and wrong when the file ran alone. What is true under the lease
        # and false without it is that a *second* process cannot be in the
        # decision at all.
        _await_signal(inside_read, what="a process to reach the claim decision")
        os.set_blocking(inside_read, False)
        time.sleep(_A_GENEROUS_MOMENT)
        try:
            gatecrasher = os.read(inside_read, 1)
        except BlockingIOError:
            gatecrasher = b""
        # Two bytes: one for the arrival above, one for a gatecrasher, so an
        # unguarded run finishes instead of hanging on the assertion below.
        os.write(release_write, b"gg")

        # waitstatus_to_exitcode, not `>> 8`. A child killed by a signal has the
        # signal number in the *low* byte, so shifting reports it as 0 — the
        # winner's code — and a crashed child would read as a second success.
        codes = sorted(
            os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in children
        )

        assert gatecrasher == b"", "both processes were inside the claim at once"
        assert codes == [0, 3], f"expected one winner and one refusal, got {codes}"
        recorded = json.loads((root / CLAIM_FILE).read_text())
        assert Path(recorded["profile_path"]).name in {"first", "second"}

    def test_a_held_browser_lease_does_not_block_a_first_claim(self, tmp_path):
        """The regression that hung the suite, and the reason for a separate lock.

        The claim used to take the *browser* lease to write its marker, which
        made starting the server depend on nobody else browsing. Measured: a
        second process started while another momentarily held the profile sat
        out the whole budget and then refused. Two unrelated resources sharing
        one lock, and a startup failure invented out of it.

        The marker is not among the artifacts rotation moves, so a claiming peer
        and a rotating peer never touch the same file and never need to wait for
        each other.
        """
        from linkedin_mcp_server.profile_lease import get_profile_lease

        target = tmp_path / "custom" / "profile"
        target.parent.mkdir(parents=True)

        holding_read, holding_write = os.pipe()
        release_read, release_write = os.pipe()
        holder_pid = os.fork()
        if holder_pid == 0:
            status = 1
            try:
                if get_profile_lease(target).try_acquire():
                    os.write(holding_write, b"1")
                    _wait_quietly(release_read)
                    status = 0
            except BaseException:
                status = 4
            finally:
                os._exit(status)

        _await_signal(holding_read, what="the holder to take the browser lease")
        started = time.monotonic()
        try:
            assert ensure_profile_claim(target) == target
        finally:
            os.write(release_write, b"g")
            os.waitpid(holder_pid, 0)

        # Elapsed time, not just success. Waiting the budget out and then
        # succeeding once the holder left would satisfy the assertion above.
        assert time.monotonic() - started < 1.0, "it queued behind the browser lease"
        assert claim_path(target).exists()

    def test_an_already_claimed_root_takes_no_lock_at_all(self, tmp_path):
        """The second, third and thousandth run have nothing left to decide.

        They must answer from the marker while other processes are busy, which
        is every run after the first.
        """
        from linkedin_mcp_server.profile_lease import acquire_locked_fd
        from linkedin_mcp_server.profile_claim import CLAIM_LOCK_FILE

        target = tmp_path / "custom" / "profile"
        ensure_profile_claim(target)

        # Hold the claim lock from a foreign process, so any attempt to take it
        # would have to wait the budget out.
        holding_read, holding_write = os.pipe()
        release_read, release_write = os.pipe()
        holder_pid = os.fork()
        if holder_pid == 0:
            status = 1
            try:
                if acquire_locked_fd(target.parent / CLAIM_LOCK_FILE, exclusive=True):
                    os.write(holding_write, b"1")
                    _wait_quietly(release_read)
                    status = 0
            except BaseException:
                status = 4
            finally:
                os._exit(status)

        _await_signal(holding_read, what="the holder to take the claim lock")
        started = time.monotonic()
        try:
            assert ensure_profile_claim(target) == target
            assert require_profile_claim(target) == target
        finally:
            os.write(release_write, b"g")
            os.waitpid(holder_pid, 0)

        assert time.monotonic() - started < 1.0, "it queued behind the claim lock"

    def test_an_unclaimed_root_being_decided_elsewhere_says_so(
        self, tmp_path, monkeypatch
    ):
        """A stuck peer ends in a message rather than a hang.

        Run on a thread and joined against a deadline, so an unbounded wait
        fails this in seconds. Called inline it would hang the whole suite until
        something outside killed it, which is a failure nobody can read.
        """
        import threading

        from linkedin_mcp_server import profile_claim as claim_module
        from linkedin_mcp_server.profile_claim import CLAIM_LOCK_FILE
        from linkedin_mcp_server.profile_lease import acquire_locked_fd

        monkeypatch.setattr(claim_module, "_CLAIM_WAIT_SECONDS", 0.2)
        target = tmp_path / "custom" / "profile"
        target.parent.mkdir(parents=True)

        holding_read, holding_write = os.pipe()
        release_read, release_write = os.pipe()
        holder_pid = os.fork()
        if holder_pid == 0:
            status = 1
            try:
                if acquire_locked_fd(target.parent / CLAIM_LOCK_FILE, exclusive=True):
                    os.write(holding_write, b"1")
                    _wait_quietly(release_read)
                    status = 0
            except BaseException:
                status = 4
            finally:
                os._exit(status)

        _await_signal(holding_read, what="the holder to take the claim lock")

        outcome: dict[str, object] = {}

        def claim_against_the_holder() -> None:
            try:
                ensure_profile_claim(target)
                outcome["claimed"] = True
            except BaseException as exc:  # noqa: BLE001 - reported, not raised
                outcome["error"] = exc

        worker = threading.Thread(target=claim_against_the_holder, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        gave_up_in_time = not worker.is_alive()

        os.write(release_write, b"g")
        os.waitpid(holder_pid, 0)
        worker.join(timeout=15)

        assert gave_up_in_time, "the wait was not bounded"
        assert isinstance(outcome.get("error"), ProfileRootRefusedError)
        assert "Another process is deciding" in str(outcome["error"])
        assert not claim_path(target).exists()
