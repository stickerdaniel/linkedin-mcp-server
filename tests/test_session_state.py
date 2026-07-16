import asyncio
import json
import multiprocessing
import os
from pathlib import Path
from uuid import UUID

import pytest

from linkedin_mcp_server.core.camoufox_identity import (
    CamoufoxIdentityError,
    load_camoufox_identity_sha256,
)
import linkedin_mcp_server.core.camoufox_identity as identity_module
from linkedin_mcp_server.session_state import (
    _INVALID_STATE_PREFIX,
    _MAX_INVALID_STATE_BACKUPS,
    acquire_pending_profile_lease,
    camoufox_identity_path,
    clear_auth_state,
    clear_runtime_profile,
    commit_source_session,
    get_runtime_id,
    get_runtime_instance_id,
    load_runtime_state,
    load_source_state,
    move_artifacts_aside,
    portable_cookie_is_valid,
    portable_cookie_path,
    runtime_dir,
    runtime_profile_dir,
    runtime_state_path,
    runtime_storage_state_path,
    rotate_runtime_instance_id,
    source_session_lock,
    source_session_lock_path,
    source_state_path,
    stage_bound_camoufox_identity,
    write_runtime_state,
    write_source_state,
)
import linkedin_mcp_server.session_state as session_state_module


def _write_valid_identity(path: Path, marker: str) -> tuple[bytes, str]:
    config = {
        "navigator.userAgent": "Mozilla/5.0 Firefox/135.0",
        "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64",
        "fonts:spacing_seed": marker,
    }
    artifact = identity_module._new_artifact(
        {
            "env": {"CAMOU_CONFIG_1": json.dumps(config, separators=(",", ":"))},
            "firefox_user_prefs": {"webgl.enable-webgl2": True},
        }
    )
    payload = json.dumps(artifact, sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload, artifact["identity_sha256"]


def _fork_runtime_instance_probe(
    source_profile_dir: Path,
    runtime_id: str,
    connection,
    release_event,
) -> None:
    """Report a forked child's runtime identity while keeping its path alive."""
    instance_id = get_runtime_instance_id()
    profile_dir = runtime_profile_dir(runtime_id, source_profile_dir)
    profile_dir.mkdir(parents=True)
    (profile_dir / "child-marker.txt").write_text("child")
    connection.send((os.getpid(), instance_id, str(profile_dir)))
    connection.close()
    release_event.wait(timeout=10)


def _source_lock_probe(
    source_profile_dir: Path,
    connection,
    release_event,
) -> None:
    """Hold the real OS advisory lock until the parent releases this process."""

    async def hold_lock() -> None:
        async with source_session_lock(source_profile_dir, timeout_seconds=2):
            connection.send("acquired")
            release_event.wait(timeout=10)

    try:
        asyncio.run(hold_lock())
    finally:
        connection.close()


def _pending_profile_lease_probe(
    pending_root: Path,
    connection,
    release_event,
) -> None:
    """Own a pending profile lease until the parent permits teardown."""
    lease = acquire_pending_profile_lease(pending_root)
    try:
        (pending_root / "profile" / "lock-marker").parent.mkdir(parents=True)
        (pending_root / "profile" / "lock-marker").write_text("possibly live")
        connection.send("acquired")
        connection.close()
        release_event.wait(timeout=10)
    finally:
        lease.release()


def _crash_during_source_commit(
    source_profile_dir: Path,
    connection,
) -> None:
    """Exit after cookie/identity promotion but before source-state publish."""

    async def crash_commit() -> None:
        async with source_session_lock(source_profile_dir, timeout_seconds=2):
            cookie_path = portable_cookie_path(source_profile_dir)
            staged_cookie = cookie_path.with_name(".crash-cookie.pending")
            staged_identity = cookie_path.with_name(".crash-identity.pending")
            staged_cookie.write_bytes(b"new cookie from killed publisher")
            _write_valid_identity(staged_identity, "killed-publisher")

            def exit_before_state(*_args, **_kwargs):
                connection.send("canonical cookie and identity promoted")
                connection.close()
                os._exit(23)

            setattr(session_state_module, "write_source_state", exit_before_state)
            commit_source_session(
                staged_cookie,
                source_profile_dir,
                staged_camoufox_identity_path=staged_identity,
            )

    asyncio.run(crash_commit())


def test_write_source_state_creates_generation(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )

    state = write_source_state(isolate_profile_dir)

    assert state.source_runtime_id == "macos-arm64-host"
    assert state.login_generation
    assert state.user_agent is None  # manual-login default
    assert source_state_path(isolate_profile_dir).exists()
    assert load_source_state(isolate_profile_dir) == state


def test_commit_source_session_rolls_back_all_artifacts_byte_for_byte(
    monkeypatch, isolate_profile_dir
):
    cookie_path = portable_cookie_path(isolate_profile_dir)
    state_path = source_state_path(isolate_profile_dir)
    identity_path = camoufox_identity_path(isolate_profile_dir)
    old_cookie = b'[{"old-cookie":"exact bytes"}]\n'
    old_state = b'{"old-state":"exact bytes"}\n'
    old_identity, _old_digest = _write_valid_identity(identity_path, "old")
    cookie_path.write_bytes(old_cookie)
    state_path.write_bytes(old_state)
    staged = cookie_path.with_name(".cookies.test-pending.json")
    staged_identity = cookie_path.with_name(".identity.test-pending.json")
    staged.write_bytes(b'[{"new-cookie":true}]')
    _new_identity, new_digest = _write_valid_identity(staged_identity, "new")

    def fail_after_overwriting_state(*args, **kwargs):
        del args, kwargs
        state_path.write_bytes(b'{"partial-new-state":true}')
        raise OSError("fault-injected source-state write")

    monkeypatch.setattr(
        session_state_module, "write_source_state", fail_after_overwriting_state
    )

    with pytest.raises(OSError, match="fault-injected"):
        commit_source_session(
            staged,
            isolate_profile_dir,
            staged_camoufox_identity_path=staged_identity,
            camoufox_identity_sha256=new_digest,
        )

    assert cookie_path.read_bytes() == old_cookie
    assert state_path.read_bytes() == old_state
    assert identity_path.read_bytes() == old_identity
    assert not staged.exists()
    assert not staged_identity.exists()
    assert not list(cookie_path.parent.glob(".source-session-transaction-*"))


def test_commit_source_session_removes_new_pair_when_no_previous_pair_exists(
    monkeypatch, isolate_profile_dir
):
    cookie_path = portable_cookie_path(isolate_profile_dir)
    state_path = source_state_path(isolate_profile_dir)
    staged = cookie_path.with_name(".cookies.test-pending.json")
    staged.write_text('[{"name":"li_at"}]')

    def fail_after_creating_state(*args, **kwargs):
        del args, kwargs
        state_path.write_text("partial")
        raise OSError("fault-injected source-state write")

    monkeypatch.setattr(
        session_state_module, "write_source_state", fail_after_creating_state
    )

    with pytest.raises(OSError, match="fault-injected"):
        commit_source_session(staged, isolate_profile_dir)

    assert not cookie_path.exists()
    assert not state_path.exists()
    assert not staged.exists()


def test_commit_rolls_back_cookie_when_identity_promotion_fails(
    monkeypatch, isolate_profile_dir
):
    cookie_path = portable_cookie_path(isolate_profile_dir)
    state_path = source_state_path(isolate_profile_dir)
    identity_path = camoufox_identity_path(isolate_profile_dir)
    old_cookie = b"old cookie bytes"
    old_state = b"old state bytes"
    old_identity, _ = _write_valid_identity(identity_path, "old")
    cookie_path.write_bytes(old_cookie)
    state_path.write_bytes(old_state)
    staged_cookie = cookie_path.with_name(".cookie.pending")
    staged_identity = cookie_path.with_name(".identity.pending")
    staged_cookie.write_bytes(b"new cookie bytes")
    _write_valid_identity(staged_identity, "new")
    real_replace = Path.replace

    def fault_injected_replace(self, target):
        if self == staged_identity:
            raise OSError("fault-injected identity promotion")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", fault_injected_replace)

    with pytest.raises(OSError, match="identity promotion"):
        commit_source_session(
            staged_cookie,
            isolate_profile_dir,
            staged_camoufox_identity_path=staged_identity,
        )

    assert cookie_path.read_bytes() == old_cookie
    assert state_path.read_bytes() == old_state
    assert identity_path.read_bytes() == old_identity
    assert not staged_cookie.exists()
    assert not staged_identity.exists()


def test_commit_rejects_corrupt_staged_identity_before_canonical_mutation(
    isolate_profile_dir,
):
    cookie_path = portable_cookie_path(isolate_profile_dir)
    state_path = source_state_path(isolate_profile_dir)
    identity_path = camoufox_identity_path(isolate_profile_dir)
    old_cookie = b"old cookie bytes"
    old_state = b"old state bytes"
    old_identity, _ = _write_valid_identity(identity_path, "old")
    cookie_path.write_bytes(old_cookie)
    state_path.write_bytes(old_state)
    staged_cookie = cookie_path.with_name(".cookie.pending")
    staged_identity = cookie_path.with_name(".identity.pending")
    staged_cookie.write_bytes(b"new cookie bytes")
    staged_identity.write_bytes(b'{"corrupt":true}')

    with pytest.raises(CamoufoxIdentityError):
        commit_source_session(
            staged_cookie,
            isolate_profile_dir,
            staged_camoufox_identity_path=staged_identity,
        )

    assert cookie_path.read_bytes() == old_cookie
    assert state_path.read_bytes() == old_state
    assert identity_path.read_bytes() == old_identity


def test_commit_derives_source_digest_from_staged_identity(isolate_profile_dir):
    cookie_path = portable_cookie_path(isolate_profile_dir)
    staged_cookie = cookie_path.with_name(".cookie.pending")
    staged_identity = cookie_path.with_name(".identity.pending")
    staged_cookie.write_bytes(b"new cookie bytes")
    identity_bytes, digest = _write_valid_identity(staged_identity, "new")

    state = commit_source_session(
        staged_cookie,
        isolate_profile_dir,
        staged_camoufox_identity_path=staged_identity,
    )

    assert state.camoufox_identity_sha256 == digest
    assert load_source_state(isolate_profile_dir) == state
    canonical_identity = camoufox_identity_path(isolate_profile_dir)
    assert canonical_identity.read_bytes() == identity_bytes
    assert load_camoufox_identity_sha256(canonical_identity) == digest


def test_patchright_commit_removes_previous_identity(isolate_profile_dir):
    identity_path = camoufox_identity_path(isolate_profile_dir)
    _write_valid_identity(identity_path, "old")
    cookie_path = portable_cookie_path(isolate_profile_dir)
    staged_cookie = cookie_path.with_name(".cookie.pending")
    staged_cookie.write_bytes(b"patchright cookie")

    state = commit_source_session(staged_cookie, isolate_profile_dir)

    assert state.camoufox_identity_sha256 is None
    assert not identity_path.exists()


def test_stage_bound_identity_ignores_digest_mismatch(isolate_profile_dir):
    canonical_identity = camoufox_identity_path(isolate_profile_dir)
    _identity_bytes, _digest = _write_valid_identity(canonical_identity, "old")
    write_source_state(
        isolate_profile_dir,
        camoufox_identity_sha256="0" * 64,
    )
    staged_identity = canonical_identity.with_name(".identity.pending")

    assert stage_bound_camoufox_identity(staged_identity, isolate_profile_dir) is None
    assert not staged_identity.exists()


def test_source_state_round_trips_user_agent(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    ua = "Mozilla/5.0 (test) Chrome/148.0.0.0"

    state = write_source_state(isolate_profile_dir, user_agent=ua)

    assert state.user_agent == ua
    assert load_source_state(isolate_profile_dir) == state


def test_source_state_round_trips_camoufox_identity_digest(
    monkeypatch, isolate_profile_dir
):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "linux-amd64-host",
    )
    digest = "c" * 64

    state = write_source_state(isolate_profile_dir, camoufox_identity_sha256=digest)

    assert state.version == 2
    assert state.camoufox_identity_sha256 == digest
    assert load_source_state(isolate_profile_dir) == state
    assert camoufox_identity_path(isolate_profile_dir) == (
        isolate_profile_dir.parent / "camoufox-identity.json"
    )


@pytest.mark.asyncio
async def test_clear_auth_state_removes_credentials_but_retains_runtime_profiles(
    isolate_profile_dir,
):
    identity_path = camoufox_identity_path(isolate_profile_dir)
    identity_path.write_text('{"private":"identity"}')
    portable_cookie_path(isolate_profile_dir).write_text("secret")
    source_state_path(isolate_profile_dir).write_text("secret state")
    runtime_marker = (
        runtime_profile_dir("linux-amd64-host", isolate_profile_dir) / "marker"
    )
    runtime_marker.parent.mkdir(parents=True)
    runtime_marker.write_text("possibly live")
    root = isolate_profile_dir.parent
    for name in (
        "invalid-state-secret",
        ".source-session-rollback-secret",
        ".source-session-transaction-secret",
        ".login-pending-secret",
        ".import-pending-secret",
    ):
        pending = root / name
        pending.mkdir()
        (pending / "cookies.json").write_text("li_at secret")

    assert await clear_auth_state(isolate_profile_dir) is True
    assert not identity_path.exists()
    assert not portable_cookie_path(isolate_profile_dir).exists()
    assert not source_state_path(isolate_profile_dir).exists()
    assert runtime_marker.read_text() == "possibly live"
    assert not list(root.glob("invalid-state-*"))
    assert not list(root.glob(".source-session-rollback-*"))
    assert not list(root.glob(".source-session-transaction-*"))
    assert not list(root.glob(".login-pending-*"))
    assert not list(root.glob(".import-pending-*"))


def test_logout_retains_pending_profile_owned_by_live_process(isolate_profile_dir):
    pending = isolate_profile_dir.parent / ".login-pending-live"
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    release_event = context.Event()
    child = context.Process(
        target=_pending_profile_lease_probe,
        args=(pending, send_connection, release_event),
    )
    child.start()
    send_connection.close()

    try:
        assert receive_connection.poll(5), "child did not acquire pending lease"
        assert receive_connection.recv() == "acquired"
        assert asyncio.run(clear_auth_state(isolate_profile_dir)) is False
        assert (pending / "profile" / "lock-marker").read_text() == "possibly live"
    finally:
        release_event.set()
        receive_connection.close()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)

    assert child.exitcode == 0
    assert asyncio.run(clear_auth_state(isolate_profile_dir)) is True
    assert not pending.exists()


def test_load_source_state_defaults_user_agent_for_old_files(
    monkeypatch, isolate_profile_dir
):
    """A pre-existing source-state.json without user_agent still loads."""
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    write_source_state(isolate_profile_dir, user_agent="Mozilla/5.0 (test)")
    payload = source_state_path(isolate_profile_dir)
    data = json.loads(payload.read_text())
    del data["user_agent"]
    payload.write_text(json.dumps(data))

    loaded = load_source_state(isolate_profile_dir)
    assert loaded is not None
    assert loaded.user_agent is None


def test_portable_cookie_is_valid_requires_nonempty_linkedin_li_at(
    isolate_profile_dir,
):
    portable_cookie_path(isolate_profile_dir).write_text(
        json.dumps(
            [
                {
                    "name": "li_at",
                    "domain": ".linkedin.com",
                    "value": "session",
                }
            ]
        )
    )

    assert portable_cookie_is_valid(isolate_profile_dir) is True


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "{}",
        "[]",
        json.dumps([{"name": "li_at", "domain": ".linkedin.com", "value": "  "}]),
        json.dumps([{"name": "li_at", "domain": ".example.com", "value": "x"}]),
        json.dumps(
            [
                {
                    "name": "li_at",
                    "domain": "linkedin.com.evil.test",
                    "value": "x",
                }
            ]
        ),
        json.dumps([{"name": "JSESSIONID", "domain": ".linkedin.com", "value": "x"}]),
    ],
)
def test_portable_cookie_is_valid_rejects_unusable_snapshots(
    isolate_profile_dir, payload
):
    portable_cookie_path(isolate_profile_dir).write_text(payload)

    assert portable_cookie_is_valid(isolate_profile_dir) is False


def test_portable_cookie_is_valid_rejects_missing_file(isolate_profile_dir):
    assert portable_cookie_is_valid(isolate_profile_dir) is False


@pytest.mark.asyncio
async def test_source_session_lock_is_reentrant_only_for_owning_task(
    isolate_profile_dir,
):
    async with source_session_lock(isolate_profile_dir):
        # Same-task nesting is safe for high-level + low-level transaction guards.
        async with source_session_lock(isolate_profile_dir, timeout_seconds=0):
            assert source_session_lock_path(isolate_profile_dir).exists()

        # ContextVars are copied into child tasks, but ownership must not be.
        async def child_attempt():
            async with source_session_lock(isolate_profile_dir, timeout_seconds=0.01):
                raise AssertionError("child task bypassed the advisory lock")

        with pytest.raises(TimeoutError, match="source-session lock"):
            await asyncio.create_task(child_attempt())

    async with source_session_lock(isolate_profile_dir, timeout_seconds=0):
        pass


@pytest.mark.asyncio
async def test_source_session_lock_rejects_negative_timeout(isolate_profile_dir):
    with pytest.raises(ValueError, match="non-negative"):
        async with source_session_lock(isolate_profile_dir, timeout_seconds=-1):
            pass


def test_source_session_lock_serializes_independent_processes(isolate_profile_dir):
    """A separate interpreter cannot bypass the advisory source-session lock."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    release_event = context.Event()
    child = context.Process(
        target=_source_lock_probe,
        args=(isolate_profile_dir, send_connection, release_event),
    )
    child.start()
    send_connection.close()

    async def acquire(timeout_seconds: float) -> None:
        async with source_session_lock(
            isolate_profile_dir,
            timeout_seconds=timeout_seconds,
        ):
            pass

    try:
        assert receive_connection.poll(5), "child did not acquire source lock"
        assert receive_connection.recv() == "acquired"
        with pytest.raises(TimeoutError, match="source-session lock"):
            asyncio.run(acquire(0.05))
    finally:
        release_event.set()
        receive_connection.close()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)

    assert child.exitcode == 0
    asyncio.run(acquire(1))


def test_source_lock_recovers_commit_interrupted_by_process_death(
    isolate_profile_dir,
):
    """A killed publisher cannot leave a mixed credential generation visible."""
    cookie_path = portable_cookie_path(isolate_profile_dir)
    state_path = source_state_path(isolate_profile_dir)
    identity_path = camoufox_identity_path(isolate_profile_dir)
    old_cookie = b"old cookie bytes"
    old_state = b"old state bytes"
    old_identity, _ = _write_valid_identity(identity_path, "old")
    cookie_path.write_bytes(old_cookie)
    state_path.write_bytes(old_state)

    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    child = context.Process(
        target=_crash_during_source_commit,
        args=(isolate_profile_dir, send_connection),
    )
    child.start()
    send_connection.close()
    try:
        assert receive_connection.poll(10), "publisher did not reach commit window"
        assert receive_connection.recv() == "canonical cookie and identity promoted"
    finally:
        receive_connection.close()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)

    assert child.exitcode == 23
    assert cookie_path.read_bytes() != old_cookie
    assert identity_path.read_bytes() != old_identity
    assert state_path.read_bytes() == old_state

    async def acquire_and_recover() -> None:
        async with source_session_lock(isolate_profile_dir, timeout_seconds=2):
            pass

    asyncio.run(acquire_and_recover())

    assert cookie_path.read_bytes() == old_cookie
    assert state_path.read_bytes() == old_state
    assert identity_path.read_bytes() == old_identity
    assert not list(cookie_path.parent.glob(".source-session-transaction-*"))


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
    instance_id = get_runtime_instance_id()
    expected_runtime_dir = (
        isolate_profile_dir.parent
        / "runtime-profiles"
        / "linux-amd64-container"
        / "instances"
        / instance_id
    )
    assert (
        runtime_storage_state_path(
            "linux-amd64-container",
            isolate_profile_dir,
        )
        == expected_runtime_dir / "storage-state.json"
    )


def test_get_runtime_instance_id_is_cached_and_regenerates_for_new_pid(monkeypatch):
    pids = iter((101, 101, 202))
    uuids = iter((UUID(int=1), UUID(int=2)))

    def fake_getpid():
        return next(pids, 202)

    monkeypatch.setattr(session_state_module.os, "getpid", fake_getpid)
    monkeypatch.setattr(session_state_module, "uuid4", lambda: next(uuids))
    monkeypatch.setattr(session_state_module, "_runtime_instance_pid", None)
    monkeypatch.setattr(session_state_module, "_runtime_instance_id", None)

    first = get_runtime_instance_id()
    cached = get_runtime_instance_id()
    after_pid_change = get_runtime_instance_id()

    assert first == f"101-{UUID(int=1).hex}"
    assert cached == first
    assert after_pid_change == f"202-{UUID(int=2).hex}"
    assert after_pid_change != first


def test_rotate_runtime_instance_id_abandons_previous_namespace(
    isolate_profile_dir,
):
    runtime_id = "linux-amd64-host"
    old_instance = get_runtime_instance_id()
    old_path = runtime_profile_dir(runtime_id, isolate_profile_dir)
    old_path.mkdir(parents=True)

    new_instance = rotate_runtime_instance_id()
    new_path = runtime_profile_dir(runtime_id, isolate_profile_dir)

    assert new_instance != old_instance
    assert new_path != old_path
    assert old_path.exists()


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="fork start method is unavailable on this platform",
)
def test_forked_process_gets_isolated_runtime_path(isolate_profile_dir):
    runtime_id = "linux-amd64-host"
    parent_pid = os.getpid()
    parent_instance_id = get_runtime_instance_id()
    parent_profile_dir = runtime_profile_dir(runtime_id, isolate_profile_dir)
    parent_profile_dir.mkdir(parents=True)
    (parent_profile_dir / "parent-marker.txt").write_text("parent")

    context = multiprocessing.get_context("fork")
    receive_connection, send_connection = context.Pipe(duplex=False)
    release_event = context.Event()
    child = context.Process(
        target=_fork_runtime_instance_probe,
        args=(
            isolate_profile_dir,
            runtime_id,
            send_connection,
            release_event,
        ),
    )
    child.start()
    send_connection.close()

    try:
        child_pid, child_instance_id, child_profile_dir_raw = receive_connection.recv()
        child_profile_dir = Path(child_profile_dir_raw)

        assert child_pid != parent_pid
        assert child_instance_id != parent_instance_id
        assert child_profile_dir != parent_profile_dir
        assert child_profile_dir == (
            isolate_profile_dir.parent
            / "runtime-profiles"
            / runtime_id
            / "instances"
            / child_instance_id
            / "profile"
        )

        assert clear_runtime_profile(runtime_id, isolate_profile_dir) is True
        assert not runtime_dir(runtime_id, isolate_profile_dir).exists()
        assert not parent_profile_dir.exists()
        assert (child_profile_dir / "child-marker.txt").read_text() == "child"
    finally:
        receive_connection.close()
        release_event.set()
        child.join(timeout=10)
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)

    assert child.exitcode == 0


def test_move_artifacts_aside_backs_up_instead_of_deleting(isolate_profile_dir):
    target = isolate_profile_dir / "marker.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("real session data")

    backup_dir = move_artifacts_aside([target], isolate_profile_dir)

    assert backup_dir is not None
    assert not target.exists()
    assert (backup_dir / "marker.txt").read_text() == "real session data"


def test_move_artifacts_aside_returns_none_when_nothing_exists(isolate_profile_dir):
    missing = isolate_profile_dir / "does-not-exist.txt"

    assert move_artifacts_aside([missing], isolate_profile_dir) is None


def test_move_artifacts_aside_uses_unique_directory_within_same_second(
    monkeypatch, isolate_profile_dir
):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.utcnow_iso",
        lambda: "2026-07-15T12:00:00Z",
    )
    first = isolate_profile_dir / "first.txt"
    second = isolate_profile_dir / "second.txt"
    first.parent.mkdir(parents=True)
    first.write_text("first")
    first_backup = move_artifacts_aside([first], isolate_profile_dir)
    second.write_text("second")
    second_backup = move_artifacts_aside([second], isolate_profile_dir)

    assert first_backup is not None
    assert second_backup is not None
    assert first_backup != second_backup
    assert (first_backup / "first.txt").read_text() == "first"
    assert (second_backup / "second.txt").read_text() == "second"


def test_move_artifacts_aside_prunes_old_backups(monkeypatch, isolate_profile_dir):
    """Regression test for unbounded backup growth: only the most recent
    _MAX_INVALID_STATE_BACKUPS survive."""
    timestamps = iter(
        f"2026-01-01T00-00-{i:02d}Z" for i in range(_MAX_INVALID_STATE_BACKUPS + 3)
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.utcnow_iso", lambda: next(timestamps)
    )

    total_backups = _MAX_INVALID_STATE_BACKUPS + 3
    for i in range(total_backups):
        target = isolate_profile_dir / f"marker-{i}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(i))
        move_artifacts_aside([target], isolate_profile_dir)

    backups = sorted(
        p
        for p in isolate_profile_dir.parent.glob(f"{_INVALID_STATE_PREFIX}*")
        if p.is_dir()
    )
    assert len(backups) == _MAX_INVALID_STATE_BACKUPS
    # The survivors are the most recent ones (highest-numbered markers).
    surviving_markers = sorted(
        p.stem.split("-")[1] for backup in backups for p in backup.iterdir()
    )
    assert surviving_markers == [
        str(i) for i in range(total_backups - _MAX_INVALID_STATE_BACKUPS, total_backups)
    ]


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
