import json

from linkedin_mcp_server.session_state import (
    fingerprint_path,
    get_or_create_fingerprint,
    get_runtime_id,
    load_source_state,
    source_state_path,
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


def test_load_source_state_ignores_unknown_fields(monkeypatch, isolate_profile_dir):
    monkeypatch.setattr(
        "linkedin_mcp_server.session_state.get_runtime_id",
        lambda: "macos-arm64-host",
    )
    state = write_source_state(isolate_profile_dir)
    payload = source_state_path(isolate_profile_dir)
    payload.write_text(payload.read_text().replace("}", ', "future_field": "keep calm"}', 1))

    assert load_source_state(isolate_profile_dir) == state


def test_get_runtime_id_host_format(monkeypatch):
    monkeypatch.setattr("linkedin_mcp_server.session_state.platform.system", lambda: "Darwin")
    monkeypatch.setattr("linkedin_mcp_server.session_state.platform.machine", lambda: "arm64")

    assert get_runtime_id() == "macos-arm64-host"


def test_get_runtime_id_linux_amd64(monkeypatch):
    monkeypatch.setattr("linkedin_mcp_server.session_state.platform.system", lambda: "Linux")
    monkeypatch.setattr("linkedin_mcp_server.session_state.platform.machine", lambda: "x86_64")

    assert get_runtime_id() == "linux-amd64-host"


def test_get_or_create_fingerprint_creates_file(isolate_profile_dir):
    fp = get_or_create_fingerprint(isolate_profile_dir)

    assert "hardwareConcurrency" in fp
    assert "deviceMemory" in fp
    assert fp["hardwareConcurrency"] in (4, 8, 10, 12, 16)
    assert fp["deviceMemory"] in (4, 8, 16, 32)
    assert fingerprint_path(isolate_profile_dir).exists()


def test_get_or_create_fingerprint_is_stable(isolate_profile_dir):
    fp1 = get_or_create_fingerprint(isolate_profile_dir)
    fp2 = get_or_create_fingerprint(isolate_profile_dir)

    assert fp1 == fp2


def test_get_or_create_fingerprint_recovers_from_corrupt_file(isolate_profile_dir):
    fp_file = fingerprint_path(isolate_profile_dir)
    fp_file.parent.mkdir(parents=True, exist_ok=True)
    fp_file.write_text("not json")

    fp = get_or_create_fingerprint(isolate_profile_dir)
    assert "hardwareConcurrency" in fp
    assert "deviceMemory" in fp


def test_get_or_create_fingerprint_respects_existing(isolate_profile_dir):
    fp_file = fingerprint_path(isolate_profile_dir)
    fp_file.parent.mkdir(parents=True, exist_ok=True)
    fp_file.write_text(json.dumps({"hardwareConcurrency": 42, "deviceMemory": 99}))

    fp = get_or_create_fingerprint(isolate_profile_dir)
    assert fp["hardwareConcurrency"] == 42
    assert fp["deviceMemory"] == 99
