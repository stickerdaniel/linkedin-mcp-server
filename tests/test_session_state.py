from linkedin_mcp_server.session_state import (
    get_runtime_id,
    load_runtime_state,
    load_source_state,
    runtime_profile_dir,
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
