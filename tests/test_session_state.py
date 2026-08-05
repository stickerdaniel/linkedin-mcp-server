import json
import os
import shutil
import socket

import pytest

from linkedin_mcp_server.profile_claim import ensure_profile_claim
from linkedin_mcp_server.session_state import (
    clear_auth_state,
    get_runtime_id,
    load_runtime_state,
    load_source_state,
    portable_cookie_path,
    quarantine_dirs,
    restore_source_profile,
    rotate_source_profile,
    runtime_profile_dir,
    runtime_profiles_root,
    runtime_state_path,
    runtime_storage_state_path,
    source_state_path,
    write_runtime_state,
    write_source_state,
)


def test_write_source_state_creates_generation(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )

    state = write_source_state(isolate_profile_dir)

    assert state.source_runtime_id == "macos-arm64-host"
    assert state.login_generation
    assert source_state_path(isolate_profile_dir).exists()
    assert load_source_state(isolate_profile_dir) == state


def test_load_source_state_ignores_a_recorded_user_agent(
    monkeypatch, isolate_profile_dir
):
    """A state file written before the UA override was removed still loads.

    Every existing installation has one of these on disk. Loading filters to
    the fields the dataclass declares, so the stale key is dropped rather than
    raising, and the next login writes the file without it.
    """
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    write_source_state(isolate_profile_dir)
    payload = source_state_path(isolate_profile_dir)
    data = json.loads(payload.read_text())
    data["user_agent"] = "Mozilla/5.0 (test) Chrome/143.0.0.0"
    payload.write_text(json.dumps(data))

    loaded = load_source_state(isolate_profile_dir)
    assert loaded is not None
    assert not hasattr(loaded, "user_agent")


def test_write_runtime_state_tracks_source_generation(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    source_state = write_source_state(isolate_profile_dir)

    storage_state_path = runtime_storage_state_path(
        "linux-amd64-container",
        isolate_profile_dir,
    )
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text("{}")

    runtime_state = write_runtime_state(
        "linux-amd64-container",
        source_state,
        storage_state_path,
        isolate_profile_dir,
    )

    assert runtime_state.source_login_generation == source_state.login_generation
    assert runtime_state.commit_method == "checkpoint_restart"
    assert runtime_state.storage_state_path == str(storage_state_path.resolve())
    assert runtime_state.committed_at
    assert runtime_state.profile_path == str(
        runtime_profile_dir("linux-amd64-container", isolate_profile_dir).resolve()
    )
    assert (
        load_runtime_state("linux-amd64-container", isolate_profile_dir)
        == runtime_state
    )


def test_load_source_state_ignores_unknown_fields(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    state = write_source_state(isolate_profile_dir)
    payload = source_state_path(isolate_profile_dir)
    payload.write_text(
        payload.read_text().replace("}", ', "future_field": "keep calm"}', 1)
    )

    assert load_source_state(isolate_profile_dir) == state


def test_load_runtime_state_ignores_unknown_fields(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    source_state = write_source_state(isolate_profile_dir)

    storage_state = runtime_storage_state_path(
        "linux-amd64-container",
        isolate_profile_dir,
    )
    storage_state.parent.mkdir(parents=True, exist_ok=True)
    storage_state.write_text("{}")
    runtime_state = write_runtime_state(
        "linux-amd64-container",
        source_state,
        storage_state,
        isolate_profile_dir,
    )
    payload = runtime_state_path("linux-amd64-container", isolate_profile_dir)
    payload.write_text(
        payload.read_text().replace("}", ', "future_field": "still fine"}', 1)
    )

    assert (
        load_runtime_state("linux-amd64-container", isolate_profile_dir)
        == runtime_state
    )


def test_write_runtime_state_accepts_explicit_created_at(
    monkeypatch, isolate_profile_dir
):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    source_state = write_source_state(isolate_profile_dir)

    storage_state_path = runtime_storage_state_path(
        "linux-amd64-container",
        isolate_profile_dir,
    )
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)
    storage_state_path.write_text("{}")

    runtime_state = write_runtime_state(
        "linux-amd64-container",
        source_state,
        storage_state_path,
        isolate_profile_dir,
        created_at="2026-03-12T17:09:00Z",
    )

    assert runtime_state.created_at == "2026-03-12T17:09:00Z"
    assert runtime_state.committed_at != runtime_state.created_at


def test_runtime_storage_state_path_uses_runtime_dir(isolate_profile_dir):
    assert runtime_storage_state_path(
        "linux-amd64-container",
        isolate_profile_dir,
    ) == (
        isolate_profile_dir.parent
        / "runtime-profiles"
        / "linux-amd64-container"
        / "storage-state.json"
    )


def test_get_runtime_id_marks_container(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.exists",
        lambda self: str(self) == "/.dockerenv",
    )

    assert get_runtime_id() == "linux-amd64-container"


def test_get_runtime_id_marks_container_from_cgroup_v2_mountinfo(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.exists",
        lambda self: str(self) == "/proc/1/mountinfo",
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.read_text",
        lambda self, *args, **kwargs: (
            "257 248 0:61 / / rw,relatime - overlay overlay "
            "rw,lowerdir=/var/lib/docker/overlay2/l"
        ),
    )

    assert get_runtime_id() == "linux-amd64-container"


def test_get_runtime_id_ignores_non_root_overlay_mounts(monkeypatch):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.exists",
        lambda self: str(self) == "/proc/1/mountinfo",
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.read_text",
        lambda self, *args, **kwargs: (
            "257 248 0:61 /var/lib/containers/storage/overlay "
            "/var/lib/containers/storage/overlay rw,relatime - overlay overlay "
            "rw,lowerdir=/var/lib/overlay-host/l"
        ),
    )

    assert get_runtime_id() == "linux-amd64-host"


def test_get_runtime_id_ignores_other_containers_on_the_host(monkeypatch):
    # The reported failure (#621). The test above already asserted that a
    # non-root overlay mount proves nothing, but it used a path under
    # /var/lib/containers, which contains no marker word — so it passed either
    # way. A Docker host's mountinfo says "docker" outright, and that is what
    # the substring scan tripped over.
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.system", lambda: "Linux"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.platform.machine", lambda: "x86_64"
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.exists",
        lambda self: str(self) == "/proc/1/mountinfo",
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.Path.read_text",
        lambda self, *args, **kwargs: (
            "30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
            "900 30 0:70 / /var/lib/docker/overlay2/abc/merged rw shared:400 "
            "- overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/X\n"
            "901 30 0:71 / /var/lib/docker/containers/dead/mounts/shm rw "
            "shared:401 - tmpfs shm rw\n"
        ),
    )

    assert get_runtime_id() == "linux-amd64-host"


def _seed_session(profile_dir, *, machine_id: str = "4663753") -> None:
    """Write the four artifacts a live source session consists of."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "Local State").write_text(
        json.dumps({"user_experience_metrics": {"machine_id": machine_id}})
    )
    portable_cookie_path(profile_dir).write_text('[{"name": "li_at"}]')
    source_state_path(profile_dir).write_text(
        json.dumps(
            {
                "version": 1,
                "source_runtime_id": "macos-arm64-host",
                "login_generation": "gen-1",
                "created_at": "2026-07-25T00:00:00Z",
                "profile_path": str(profile_dir),
                "cookies_path": str(portable_cookie_path(profile_dir)),
            }
        )
    )
    (runtime_profiles_root(profile_dir) / "macos-arm64-host").mkdir(parents=True)


class TestRotateSourceProfile:
    def test_retires_every_artifact(self, isolate_profile_dir):
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)

        backup = rotate_source_profile(profile_dir)

        assert backup is not None
        assert not profile_dir.exists()
        assert not portable_cookie_path(profile_dir).exists()
        assert not source_state_path(profile_dir).exists()
        assert not runtime_profiles_root(profile_dir).exists()
        assert {path.name for path in backup.iterdir()} == {
            "profile",
            "cookies.json",
            "source-state.json",
            "runtime-profiles",
        }

    def test_new_profile_does_not_inherit_the_fingerprint(self, isolate_profile_dir):
        """The point of rotating: Chromium mints machine_id once per profile
        directory, so reusing the directory hands LinkedIn one device identity
        for two accounts."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir, machine_id="4663753")

        backup = rotate_source_profile(profile_dir)

        assert backup is not None
        assert "4663753" in (backup / "profile" / "Local State").read_text()
        assert not (profile_dir / "Local State").exists()

    def test_no_op_without_a_session(self, isolate_profile_dir):
        assert rotate_source_profile(isolate_profile_dir) is None
        assert quarantine_dirs(isolate_profile_dir) == []

    def test_refuses_while_another_process_holds_the_profile(self, isolate_profile_dir):
        """Rotating underneath a live Chromium — a second CLI run, or a
        container sharing the mounted auth root — corrupts both sessions."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)
        # Chromium encodes the owning <host>-<pid> in the lock's symlink target.
        (profile_dir / "SingletonLock").symlink_to(
            f"{socket.gethostname()}-{os.getpid()}"
        )

        with pytest.raises(RuntimeError, match="in use by another process"):
            rotate_source_profile(profile_dir)

        assert profile_dir.exists()
        assert quarantine_dirs(profile_dir) == []

    def test_stale_lock_does_not_block_forever(self, isolate_profile_dir):
        """Chromium leaves its lock behind on a crash. Treating that as a live
        owner would wedge every future login behind a manual file deletion."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)
        (profile_dir / "SingletonLock").symlink_to(f"{socket.gethostname()}-999999")

        assert rotate_source_profile(profile_dir) is not None

    def test_same_second_rotations_do_not_collide(self, isolate_profile_dir):
        """utcnow_iso() is second-resolution and rotation is routine now, so
        two rotations can share a timestamp without sharing a directory."""
        profile_dir = isolate_profile_dir

        _seed_session(profile_dir)
        first = rotate_source_profile(profile_dir)
        _seed_session(profile_dir)
        second = rotate_source_profile(profile_dir)

        assert first != second
        assert len(quarantine_dirs(profile_dir)) == 2

    def test_move_failure_raises_instead_of_half_rotating(
        self, isolate_profile_dir, monkeypatch
    ):
        """A swallowed failure would leave one session split across two
        quarantines the next time around, so a partial move must be rolled
        back before the error propagates."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)
        real_move = shutil.move
        calls = {"n": 0}

        def explode_on_second(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("device busy")
            return real_move(src, dst)

        monkeypatch.setattr(
            "linkedin_mcp_server.session_state.shutil.move", explode_on_second
        )

        with pytest.raises(OSError):
            rotate_source_profile(profile_dir)

        assert calls["n"] >= 2, "the first move must succeed, so a rollback is needed"
        # Everything is back where it started, and no quarantine holds a fragment.
        assert (profile_dir / "Local State").exists()
        assert portable_cookie_path(profile_dir).exists()
        assert source_state_path(profile_dir).exists()
        assert runtime_profiles_root(profile_dir).exists()
        assert quarantine_dirs(profile_dir) == []


class TestClearAuthState:
    def test_removes_quarantined_sessions_too(self, isolate_profile_dir):
        """--logout advertises clearing all stored auth state, and a quarantine
        holds a previous session's cookies."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)
        rotate_source_profile(profile_dir)
        _seed_session(profile_dir)

        assert clear_auth_state(profile_dir) is True

        assert quarantine_dirs(profile_dir) == []
        assert not profile_dir.exists()


class TestRestoreSourceProfile:
    def test_puts_a_retired_session_back(self, isolate_profile_dir):
        """Rotation happens before the replacement exists, so a login that is
        cancelled must not leave the user logged out of a working session."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir)
        backup = rotate_source_profile(profile_dir)
        assert backup is not None

        assert restore_source_profile(backup) is True

        assert (profile_dir / "Local State").exists()
        assert portable_cookie_path(profile_dir).exists()
        assert source_state_path(profile_dir).exists()
        assert quarantine_dirs(profile_dir) == []

    def test_restores_into_the_rotated_dir_not_the_configured_one(
        self, isolate_profile_dir, tmp_path
    ):
        """--user-data-dir can point away from the configured default, and
        restoring to the configured one would strand the session elsewhere."""
        elsewhere = tmp_path / "elsewhere" / "profile"
        # A second --user-data-dir is a second auth root, and each one is
        # claimed on its own before anything may be moved out of it.
        ensure_profile_claim(elsewhere)
        _seed_session(elsewhere)
        backup = rotate_source_profile(elsewhere)
        assert backup is not None

        assert restore_source_profile(backup, elsewhere) is True

        assert (elsewhere / "Local State").exists()
        assert not (isolate_profile_dir / "Local State").exists()

    def test_restores_over_an_abandoned_profile_dir(self, isolate_profile_dir):
        """Chromium creates the profile directory on launch and leaves it behind
        when the login is cancelled. Reading that debris as a replacement would
        strand the working session in quarantine — exactly the data loss the
        restore exists to prevent."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir, machine_id="working")
        backup = rotate_source_profile(profile_dir)
        assert backup is not None
        # What a cancelled login leaves: a profile dir, but no source-state.json.
        (profile_dir / "Default").mkdir(parents=True)
        (profile_dir / "Local State").write_text("{}")

        assert restore_source_profile(backup, profile_dir) is True

        assert "working" in (profile_dir / "Local State").read_text()
        assert source_state_path(profile_dir).exists()

    def test_refuses_to_overwrite_a_newer_session(self, isolate_profile_dir):
        """If the replacement did land, restoring would mix the old profile's
        fingerprint back into the new account's session."""
        profile_dir = isolate_profile_dir
        _seed_session(profile_dir, machine_id="old")
        backup = rotate_source_profile(profile_dir)
        assert backup is not None
        _seed_session(profile_dir, machine_id="new")

        assert restore_source_profile(backup) is False

        assert "new" in (profile_dir / "Local State").read_text()


def test_rotation_refuses_while_a_container_holds_a_derived_profile(
    isolate_profile_dir,
):
    """Docker runs Chromium out of runtime-profiles/<runtime>/profile while
    sharing the mounted auth root, and rotation moves that tree too. Checking
    only the source profile would pull it out from under a live container."""
    profile_dir = isolate_profile_dir
    _seed_session(profile_dir)
    derived = runtime_profiles_root(profile_dir) / "linux-amd64-container" / "profile"
    derived.mkdir(parents=True)
    (derived / "SingletonLock").symlink_to(f"{socket.gethostname()}-{os.getpid()}")

    with pytest.raises(RuntimeError, match="in use by another process"):
        rotate_source_profile(profile_dir)

    assert profile_dir.exists()


def test_restore_ignores_a_half_written_marker(isolate_profile_dir):
    """A marker without the profile and cookies it describes is debris, and
    reading it as a committed replacement would strand the working session."""
    profile_dir = isolate_profile_dir
    _seed_session(profile_dir, machine_id="working")
    backup = rotate_source_profile(profile_dir)
    assert backup is not None
    source_state_path(profile_dir).write_text("{}")

    assert restore_source_profile(backup, profile_dir) is True

    assert "working" in (profile_dir / "Local State").read_text()


def test_lock_from_another_host_counts_as_held(isolate_profile_dir):
    """A container writing into the mounted auth root records its own hostname
    and a pid from its namespace. Probing that pid against this host is
    meaningless, so an unverifiable owner must block the rotation rather than
    let it move a live profile."""
    profile_dir = isolate_profile_dir
    _seed_session(profile_dir)
    (profile_dir / "SingletonLock").symlink_to("some-container-1")

    with pytest.raises(RuntimeError, match="in use by another process"):
        rotate_source_profile(profile_dir)

    assert profile_dir.exists()


def test_uncommitted_debris_is_parked_not_deleted(isolate_profile_dir):
    """An uncommitted profile may still hold a Chromium login worth inspecting,
    and deleting it leaves no way back if a later move fails."""
    profile_dir = isolate_profile_dir
    _seed_session(profile_dir, machine_id="working")
    backup = rotate_source_profile(profile_dir)
    assert backup is not None
    profile_dir.mkdir(parents=True)
    (profile_dir / "Local State").write_text('{"debris": true}')

    assert restore_source_profile(backup, profile_dir) is True

    assert "working" in (profile_dir / "Local State").read_text()
    parked = backup.parent / f"{backup.name}-superseded"
    assert '{"debris": true}' in (parked / "profile" / "Local State").read_text()


def test_socket_and_cookie_links_are_not_read_as_owners(isolate_profile_dir):
    """Only SingletonLock encodes <host>-<pid>. SingletonSocket holds a path and
    SingletonCookie an opaque token, so parsing them the same way turns a
    hyphen in TMPDIR into a phantom foreign host and wedges every login."""
    profile_dir = isolate_profile_dir
    _seed_session(profile_dir)
    # A dead pid on this host: the profile is genuinely free.
    (profile_dir / "SingletonLock").symlink_to(f"{socket.gethostname()}-999999")
    (profile_dir / "SingletonSocket").symlink_to("/custom-temp/chromium-x/Socket")
    (profile_dir / "SingletonCookie").symlink_to("10001647406132631536")

    assert rotate_source_profile(profile_dir) is not None


class TestContainerDetection:
    """Whether this process is inside a container, and what it costs to be wrong.

    Both directions are damaging and neither is symmetric. Reading a host as a
    container is unrecoverable for the user: every tool call answers "run
    --login on the host machine" while a perfectly valid session sits on disk,
    and no flag reached that decision before this. Reading a container as a
    host sends it looking for a browser keychain that is not there.
    """

    def _probe(self, tmp_path, content: str):
        path = tmp_path / "cgroup"
        path.write_text(content, encoding="utf-8")
        return path

    @pytest.mark.parametrize(
        ("label", "cgroup"),
        [
            ("a user session", "0::/user.slice/user-1000.slice/session-3.scope\n"),
            # The reported bug. systemd escapes dashes, so an ordinary desktop
            # app's unit contains the word "docker".
            (
                "an app whose unit is named after docker",
                "0::/user.slice/user-1000.slice/app-docker\\x2ddesktop.scope\n",
            ),
            # The machine most likely to be misread: it runs the daemon that
            # runs everyone else's containers.
            ("the docker daemon itself", "0::/system.slice/docker.service\n"),
            ("the containerd service", "0::/system.slice/containerd.service\n"),
            # A host service that merely starts with the runtime's name. An
            # earlier cut of this fix matched any "docker-" prefix and read
            # this as a container; found on a real host.
            (
                "a backup job named after docker",
                "0::/system.slice/docker-backup.scope\n",
            ),
            # Contrived, but it is what a length bound has to survive: every
            # letter happens to be a-f. Real runtimes write a full container
            # id, measured at 64 characters for a Docker systemd scope.
            (
                "a host unit whose name is accidentally hex",
                "0::/system.slice/docker-beefcafedeadbeef.scope\n",
            ),
            (
                "cgroup v1 with no container",
                "12:pids:/user.slice\n1:name=systemd:/user.slice/session-2.scope\n",
            ),
        ],
    )
    def test_a_host_is_not_read_as_a_container(self, tmp_path, label, cgroup):
        from linkedin_mcp_server.session_state import _cgroup_path_is_containerised

        assert not _cgroup_path_is_containerised(self._probe(tmp_path, cgroup)), label

    @pytest.mark.parametrize(
        ("label", "cgroup"),
        [
            (
                "docker, cgroup v1",
                "12:pids:/docker/3f2a\n1:name=systemd:/docker/3f2a\n",
            ),
            ("docker, cgroup v2", "0::/docker/3f2abc\n"),
            (
                "docker with the systemd cgroup driver",
                "0::/system.slice/docker-" + "3f2a" * 16 + ".scope\n",
            ),
            ("kubernetes", "0::/kubepods/besteffort/pod123/abc\n"),
            (
                "kubernetes on systemd",
                "0::/kubepods.slice/kubepods-burstable.slice/x.scope\n",
            ),
            ("podman", "0::/libpod_parent/libpod-" + "ab12" * 16 + "\n"),
            (
                "rootless podman",
                "0::/user.slice/user-1000.slice/libpod-" + "ab12" * 16 + ".scope\n",
            ),
            ("containerd", "0::/containerd/abcdef\n"),
            ("cri-o", "0::/system.slice/crio-" + "cd34" * 16 + ".scope\n"),
            # containerd's default namespace, which a plain `ctr run` writes.
            ("raw containerd", "0::/moby/some-container-name\n"),
            # LXC and nspawn name the segment after the container. Before this
            # module stopped reading names, an LXC container called
            # "docker-builder" matched for entirely the wrong reason — and a
            # plainly named one did not match at all.
            ("lxc", "0::/lxc.payload.web/init.scope\n"),
            ("lxc named after docker", "0::/lxc.payload.docker-builder/init.scope\n"),
            ("systemd-nspawn", "0::/machine.slice/machine-demo.scope\n"),
        ],
    )
    def test_a_container_is_still_detected(self, tmp_path, label, cgroup):
        from linkedin_mcp_server.session_state import _cgroup_path_is_containerised

        assert _cgroup_path_is_containerised(self._probe(tmp_path, cgroup)), label

    def test_mounts_belonging_to_other_containers_prove_nothing(self, tmp_path):
        # The root cause. mountinfo lists everything the namespace can see, so
        # a workstation running unrelated containers for a local database has
        # their overlay mounts in the file. Only our own root mount counts.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
            "900 30 0:70 / /var/lib/docker/overlay2/abc/merged rw shared:400 - overlay overlay rw\n"
            "901 30 0:71 / /var/lib/docker/containers/dead/mounts/shm rw shared:401 - tmpfs shm rw\n",
            encoding="utf-8",
        )

        assert not _root_mount_uses_overlay(path)

    def test_a_non_overlay_container_root_still_counts(self, tmp_path):
        # containerd with the native snapshotter bind-mounts the rootfs from
        # its own storage onto whatever filesystem the host uses — btrfs here.
        # Checking only for an overlay filesystem type read this as a host,
        # which is the dangerous direction: the container would then hunt for a
        # browser keychain that does not exist. Measured on a real container.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "1 0 0:5 /var/lib/containerd/io.containerd.snapshotter.v1.native"
            "/snapshots/12 / rw,relatime - btrfs /dev/vda1 rw,subvol=/@\n",
            encoding="utf-8",
        )

        assert _root_mount_uses_overlay(path)

    def test_a_host_root_on_the_same_filesystem_does_not(self, tmp_path):
        # The mirror image, and the reason the check reads the root line only:
        # this host has containerd storage on it, just not as its own root.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "30 1 0:5 / / rw,relatime shared:1 - btrfs /dev/vda1 rw,subvol=/@\n"
            "900 30 0:70 /var/lib/containerd/snapshots/9 /run/x rw - overlay overlay rw\n",
            encoding="utf-8",
        )

        assert not _root_mount_uses_overlay(path)

    def test_our_own_overlay_root_is_a_container(self, tmp_path):
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "1 0 0:70 / / rw,relatime - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/X\n",
            encoding="utf-8",
        )

        assert _root_mount_uses_overlay(path)

    @pytest.mark.parametrize("value", ["true", "1", "yes", "on"])
    def test_the_override_can_force_container(self, monkeypatch, value):
        monkeypatch.setenv("LINKEDIN_MCP_CONTAINER", value)

        assert get_runtime_id().endswith("-container")

    @pytest.mark.parametrize("value", ["false", "0", "no", "off"])
    def test_the_override_can_force_host(self, monkeypatch, value):
        # The half that unblocks the reported bug without editing source.
        monkeypatch.setenv("LINKEDIN_MCP_CONTAINER", value)

        assert get_runtime_id().endswith("-host")

    def test_an_unreadable_override_falls_back_to_detection(
        self, tmp_path, monkeypatch
    ):
        # Refusing to guess beats crashing the server over a typo in an
        # environment variable. Asserted against a known-container fixture:
        # every runtime id ends in -host or -container, so accepting either
        # would pass no matter what the override did.
        import linkedin_mcp_server.session_state as state

        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/docker/3f2abc\n", encoding="utf-8")
        monkeypatch.setattr(state, "_CGROUP_PROBES", (cgroup,))
        monkeypatch.setattr(state, "_MOUNTINFO_PROBES", ())
        monkeypatch.setenv("LINKEDIN_MCP_CONTAINER", "maybe")

        assert state._container_override() is None
        assert get_runtime_id().endswith("-container")

    def test_a_systemd_container_marker_is_not_trusted_on_its_own(
        self, tmp_path, monkeypatch
    ):
        # systemd's container interface answers "is there a boundary", which is
        # not the question. An OrbStack Linux machine reports lxc there and is
        # a full system with its own systemd and a persistent disk — measured.
        # Believing the marker classified it as a container and put it back
        # into "run --login on the host machine", which is this bug again.
        import linkedin_mcp_server.session_state as state

        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/.lxc\n", encoding="utf-8")
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            "30 1 0:5 /scon/containers/abc/rootfs / rw,relatime "
            "- btrfs /dev/vdb1 rw,subvol=/@\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(state, "_CGROUP_PROBES", (cgroup,))
        monkeypatch.setattr(state, "_MOUNTINFO_PROBES", (mountinfo,))

        assert state._is_container_runtime() is False

    def test_a_nested_lxc_container_is_found_by_its_root(self, tmp_path):
        # Measured on LXC 5.0.3 inside an OrbStack machine: the documented
        # lxc.payload.<name> cgroup prefix does not appear when the host is
        # itself containerised — outer machine and nested container both read
        # 0::/.lxc. The kernel's mount root still separates them, because only
        # the nested one is rooted under the container's rootfs.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "1 0 0:5 /scon/containers/orb1/rootfs/var/lib/lxc/demo/rootfs / "
            "rw,relatime - btrfs /dev/vdb1 rw,subvol=/@\n",
            encoding="utf-8",
        )

        assert _root_mount_uses_overlay(path)

    def test_the_machine_hosting_that_lxc_container_is_not_one(self, tmp_path):
        # The other half, and the reason the systemd marker is not consulted:
        # this reports container=lxc while being a full system with its own
        # systemd and a persistent disk. Believing it blocked every tool call.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "1 0 0:5 /scon/containers/orb1/rootfs / rw,relatime "
            "- btrfs /dev/vdb1 rw,subvol=/@\n",
            encoding="utf-8",
        )

        assert not _root_mount_uses_overlay(path)

    def test_an_nfs_root_is_not_a_container(self, tmp_path):
        # The mount source is whatever the far end calls its export. A server
        # exporting /var/lib/containers/workstations/alice is describing
        # somebody's laptop, and reading it turned a legitimate host into the
        # exact misdetection this fix exists to remove.
        from linkedin_mcp_server.session_state import _root_mount_uses_overlay

        path = tmp_path / "mountinfo"
        path.write_text(
            "30 1 0:42 / / rw,relatime - nfs4 "
            "nas:/var/lib/containers/workstations/alice rw\n",
            encoding="utf-8",
        )

        assert not _root_mount_uses_overlay(path)

    def test_the_whole_decision_survives_a_host_running_containers(
        self, tmp_path, monkeypatch
    ):
        # The reported machine, end to end through the function that actually
        # decides, rather than through its helpers. A Linux workstation whose
        # kernel files mention docker only because unrelated containers run on
        # it must come out as a host.
        import linkedin_mcp_server.session_state as state

        cgroup = tmp_path / "cgroup"
        cgroup.write_text(
            "0::/user.slice/user-1000.slice/session-3.scope\n", encoding="utf-8"
        )
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            "30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n"
            "900 30 0:70 / /var/lib/docker/overlay2/abc/merged rw shared:400 - overlay overlay rw\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(state, "_CGROUP_PROBES", (cgroup,))
        monkeypatch.setattr(state, "_MOUNTINFO_PROBES", (mountinfo,))

        assert state._is_container_runtime() is False

    def test_the_whole_decision_still_finds_a_real_container(
        self, tmp_path, monkeypatch
    ):
        import linkedin_mcp_server.session_state as state

        cgroup = tmp_path / "cgroup"
        cgroup.write_text("0::/docker/3f2abc\n", encoding="utf-8")
        mountinfo = tmp_path / "mountinfo"
        mountinfo.write_text(
            "30 1 259:2 / / rw,relatime shared:1 - ext4 /dev/nvme0n1p2 rw\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(state, "_CGROUP_PROBES", (cgroup,))
        monkeypatch.setattr(state, "_MOUNTINFO_PROBES", (mountinfo,))

        assert state._is_container_runtime() is True
