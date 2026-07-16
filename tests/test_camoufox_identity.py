"""Regression tests for stable, sanitized Camoufox launch identities."""

import asyncio
import json
import multiprocessing
import os
import stat
from pathlib import Path

import pytest

import linkedin_mcp_server.core.camoufox_identity as identity_module
from linkedin_mcp_server.core.camoufox_identity import (
    CamoufoxIdentityError,
    load_camoufox_identity_sha256,
    prepare_camoufox_launch_options,
)


def _options(
    marker: str,
    *,
    profile: str = "/runtime/profile",
    secret: str = "do-not-persist",
    webgl2: bool = True,
) -> dict:
    config = {
        "navigator.userAgent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:135.0) Gecko/20100101 Firefox/135.0"
        ),
        "navigator.platform": "Linux x86_64",
        "navigator.oscpu": "Linux x86_64",
        "canvas:seed": marker,
        "fonts:spacing_seed": f"font-{marker}",
        "geolocation:latitude": 10.0 if marker == "first" else 20.0,
        "geolocation:longitude": 30.0 if marker == "first" else 40.0,
        "webrtc:ipv4": "192.0.2.1" if marker == "first" else "192.0.2.2",
        "timezone": "Etc/First" if marker == "first" else "Etc/Second",
        "locale:region": "AA" if marker == "first" else "BB",
        "addons": [f"/{marker}/addon.xpi"],
    }
    if marker == "first":
        config["humanize"] = True
        config["humanize:maxTime"] = 1.5
    raw = json.dumps(config, separators=(",", ":"))
    midpoint = max(1, len(raw) // 2)
    return {
        "headless": marker == "second",
        "user_data_dir": profile,
        "proxy": {"server": f"http://proxy-{marker}.invalid"},
        "env": {
            "CURRENT_SECRET": secret,
            "CURRENT_MARKER": marker,
            "CAMOU_CONFIG_1": raw[:midpoint],
            "CAMOU_CONFIG_2": raw[midpoint:],
        },
        "firefox_user_prefs": {
            "webgl.enable-webgl2": webgl2,
            "permissions.default.image": 2 if marker == "second" else 1,
        },
    }


def _decoded_config(options: dict) -> dict:
    chunks = sorted(
        (
            (int(key.rsplit("_", 1)[1]), value)
            for key, value in options["env"].items()
            if key.startswith("CAMOU_CONFIG_")
        ),
        key=lambda item: item[0],
    )
    return json.loads("".join(value for _, value in chunks))


def _process_prepare_identity(
    identity_path: Path,
    marker: str,
    start_event,
    connection,
) -> None:
    async def prepare() -> None:
        start_event.wait(timeout=10)
        prepared, digest = await prepare_camoufox_launch_options(
            _options(marker, profile=f"/{marker}/profile"), identity_path
        )
        connection.send((_decoded_config(prepared), digest, prepared["user_data_dir"]))

    try:
        asyncio.run(prepare())
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_first_identity_persists_only_sanitized_fingerprint(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    options = _options("first", secret="super-secret-proxy-password")

    prepared, digest = await prepare_camoufox_launch_options(options, identity_path)

    payload_text = identity_path.read_text()
    payload = json.loads(payload_text)
    assert set(payload) == {
        "camoufox_version",
        "config_chunks",
        "firefox_preferences",
        "firefox_user_agent",
        "format",
        "identity_sha256",
        "platform",
        "version",
    }
    assert "super-secret-proxy-password" not in payload_text
    assert "CURRENT_SECRET" not in payload_text
    assert "env" not in payload
    persisted_config = json.loads("".join(payload["config_chunks"]))
    assert persisted_config["canvas:seed"] == "first"
    assert not any(
        key == "humanize"
        or key.startswith(("humanize:", "geolocation:", "webrtc:", "locale:"))
        or key in {"timezone", "addons"}
        for key in persisted_config
    )
    assert payload["identity_sha256"] == digest
    assert prepared["env"]["CURRENT_SECRET"] == "super-secret-proxy-password"
    assert _decoded_config(prepared)["canvas:seed"] == "first"
    if os.name != "nt":
        assert stat.S_IMODE(identity_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_reuse_keeps_identity_but_preserves_current_dynamic_options(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    first, first_digest = await prepare_camoufox_launch_options(
        _options("first", webgl2=False), identity_path
    )
    current = _options(
        "second",
        profile="/new/isolated/profile",
        secret="current-only-secret",
        webgl2=True,
    )
    current_config = _decoded_config(current)
    current_config["navigator.globalPrivacyControl"] = True
    current["env"] = {
        **{
            key: value
            for key, value in current["env"].items()
            if not key.startswith("CAMOU_CONFIG_")
        },
        "CAMOU_CONFIG_1": json.dumps(current_config),
    }

    reused, reused_digest = await prepare_camoufox_launch_options(
        current,
        identity_path,
        expected_identity_sha256=first_digest,
    )

    assert reused_digest == first_digest
    first_config = _decoded_config(first)
    reused_config = _decoded_config(reused)
    # Identity and noise seeds stay bound to the first source launch.
    assert reused_config["canvas:seed"] == first_config["canvas:seed"] == "first"
    assert reused_config["fonts:spacing_seed"] == "font-first"
    # Runtime policy and network-derived fields come from the second launch.
    assert "humanize" not in reused_config
    assert "humanize:maxTime" not in reused_config
    assert reused_config["geolocation:latitude"] == 20.0
    assert reused_config["geolocation:longitude"] == 40.0
    assert reused_config["webrtc:ipv4"] == "192.0.2.2"
    assert reused_config["timezone"] == "Etc/Second"
    assert reused_config["locale:region"] == "BB"
    assert reused_config["addons"] == ["/second/addon.xpi"]
    # Random identity fields present only in the fresh generation cannot leak
    # around the persisted artifact.
    assert "navigator.globalPrivacyControl" not in reused_config
    assert reused["firefox_user_prefs"]["webgl.enable-webgl2"] is False
    # Non-identity preferences and all launch-time options remain current.
    assert reused["firefox_user_prefs"]["permissions.default.image"] == 2
    assert reused["user_data_dir"] == "/new/isolated/profile"
    assert reused["headless"] is True
    assert reused["proxy"] == {"server": "http://proxy-second.invalid"}
    assert reused["env"]["CURRENT_SECRET"] == "current-only-secret"
    assert reused["env"]["CURRENT_MARKER"] == "second"


@pytest.mark.asyncio
async def test_corrupt_identity_fails_closed_without_overwrite(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    corrupt = b'{"not":"complete"}\n'
    identity_path.write_bytes(corrupt)

    with pytest.raises(CamoufoxIdentityError, match="format"):
        await prepare_camoufox_launch_options(_options("new"), identity_path)

    assert identity_path.read_bytes() == corrupt


@pytest.mark.asyncio
async def test_tampered_identity_checksum_fails_closed(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    await prepare_camoufox_launch_options(_options("first"), identity_path)
    payload = json.loads(identity_path.read_text())
    config = json.loads("".join(payload["config_chunks"]))
    config["canvas:seed"] = "tampered"
    payload["config_chunks"] = [json.dumps(config)]
    identity_path.write_text(json.dumps(payload))

    with pytest.raises(CamoufoxIdentityError, match="checksum"):
        await prepare_camoufox_launch_options(_options("second"), identity_path)


@pytest.mark.asyncio
async def test_missing_expected_identity_fails_without_creating_one(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"

    with pytest.raises(CamoufoxIdentityError, match="missing"):
        await prepare_camoufox_launch_options(
            _options("new"),
            identity_path,
            expected_identity_sha256="a" * 64,
        )

    assert not identity_path.exists()


@pytest.mark.asyncio
async def test_mismatched_source_generation_digest_fails_closed(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    await prepare_camoufox_launch_options(_options("first"), identity_path)

    with pytest.raises(CamoufoxIdentityError, match="source session generation"):
        await prepare_camoufox_launch_options(
            _options("second"),
            identity_path,
            expected_identity_sha256="0" * 64,
        )


@pytest.mark.asyncio
async def test_incompatible_camoufox_version_fails_closed(tmp_path, monkeypatch):
    identity_path = tmp_path / "camoufox-identity.json"
    await prepare_camoufox_launch_options(_options("first"), identity_path)
    monkeypatch.setattr(identity_module, "_camoufox_version", lambda: "999.0")

    with pytest.raises(CamoufoxIdentityError, match="package version"):
        await prepare_camoufox_launch_options(_options("second"), identity_path)


@pytest.mark.asyncio
async def test_incompatible_browser_user_agent_fails_closed(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    await prepare_camoufox_launch_options(_options("first"), identity_path)
    current = _options("second")
    config = _decoded_config(current)
    config["navigator.userAgent"] = config["navigator.userAgent"].replace(
        "Firefox/135.0", "Firefox/136.0"
    )
    current["env"] = {
        **{
            key: value
            for key, value in current["env"].items()
            if not key.startswith("CAMOU_CONFIG_")
        },
        "CAMOU_CONFIG_1": json.dumps(config),
    }

    with pytest.raises(CamoufoxIdentityError, match="browser build"):
        await prepare_camoufox_launch_options(current, identity_path)


@pytest.mark.asyncio
async def test_same_browser_build_accepts_random_linux_ua_variant(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"
    _, digest = await prepare_camoufox_launch_options(_options("first"), identity_path)
    current = _options("second")
    config = _decoded_config(current)
    config["navigator.userAgent"] = config["navigator.userAgent"].replace(
        "X11; Linux x86_64", "X11; Ubuntu; Linux x86_64"
    )
    current["env"] = {
        **{
            key: value
            for key, value in current["env"].items()
            if not key.startswith("CAMOU_CONFIG_")
        },
        "CAMOU_CONFIG_1": json.dumps(config),
    }

    prepared, reused_digest = await prepare_camoufox_launch_options(
        current,
        identity_path,
        expected_identity_sha256=digest,
    )

    assert reused_digest == digest
    assert "Ubuntu" not in _decoded_config(prepared)["navigator.userAgent"]


@pytest.mark.asyncio
async def test_same_process_concurrent_first_launches_converge(tmp_path):
    identity_path = tmp_path / "camoufox-identity.json"

    (first, first_digest), (second, second_digest) = await asyncio.gather(
        prepare_camoufox_launch_options(_options("first"), identity_path),
        prepare_camoufox_launch_options(_options("second"), identity_path),
    )

    assert first_digest == second_digest == load_camoufox_identity_sha256(identity_path)
    assert (
        _decoded_config(first)["canvas:seed"] == _decoded_config(second)["canvas:seed"]
    )
    assert _decoded_config(first)["geolocation:latitude"] == 10.0
    assert _decoded_config(second)["geolocation:latitude"] == 20.0
    assert first["user_data_dir"] == "/runtime/profile"
    assert second["user_data_dir"] == "/runtime/profile"


def test_independent_processes_converge_on_one_identity(tmp_path):
    """The adjacent advisory lock protects first-writer creation cross-process."""
    context = multiprocessing.get_context("spawn")
    identity_path = tmp_path / "camoufox-identity.json"
    start_event = context.Event()
    processes = []
    receivers = []
    for marker in ("first", "second"):
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_process_prepare_identity,
            args=(identity_path, marker, start_event, sender),
        )
        process.start()
        sender.close()
        processes.append(process)
        receivers.append(receiver)

    start_event.set()
    try:
        results = []
        for receiver in receivers:
            assert receiver.poll(10), "identity worker did not finish"
            results.append(receiver.recv())
    finally:
        for receiver in receivers:
            receiver.close()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)

    assert all(process.exitcode == 0 for process in processes)
    assert results[0][0]["canvas:seed"] == results[1][0]["canvas:seed"]
    assert {
        results[0][0]["geolocation:latitude"],
        results[1][0]["geolocation:latitude"],
    } == {
        10.0,
        20.0,
    }
    assert results[0][1] == results[1][1]
    # Each process still launched its own current isolated profile.
    assert {results[0][2], results[1][2]} == {
        "/first/profile",
        "/second/profile",
    }
