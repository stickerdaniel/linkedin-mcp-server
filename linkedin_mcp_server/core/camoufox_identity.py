"""Persistent, sanitized Camoufox fingerprint identities.

Camoufox generates a complete fingerprint (including random noise seeds) on
every call to :func:`camoufox.utils.launch_options`.  A LinkedIn cookie minted
by one launch must not be replayed by a later launch with a different identity.

Only the identity-bearing fields extracted from Camoufox's
``CAMOU_CONFIG_N`` payloads and the one Firefox preference coupled to the
sampled WebGL fingerprint are persisted here. Operational config fields such
as humanization, geolocation, WebRTC IP, locale, timezone, and addons are
merged from the current launch. The rest of the Playwright options -- notably
the process environment, proxy credentials, profile path, executable path,
headless mode, and timing settings -- also always remain current. This keeps
secrets out of the artifact without freezing runtime policy into an identity.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import errno
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
from typing import Any, AsyncIterator, BinaryIO

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
)

_ARTIFACT_FORMAT = "linkedin-mcp-camoufox-identity"
_ARTIFACT_VERSION = 2
_CONFIG_ENV_RE = re.compile(r"^CAMOU_CONFIG_([1-9][0-9]*)$")
_FIREFOX_VERSION_RE = re.compile(r"\bFirefox/([0-9]+(?:\.[0-9]+)*)\b")
_DYNAMIC_CONFIG_KEYS = frozenset({"addons", "allowMainWorld", "timezone"})
_DYNAMIC_CONFIG_PREFIXES = ("geolocation:", "humanize:", "locale:", "webrtc:")
_IDENTITY_FIREFOX_PREFS = frozenset({"webgl.enable-webgl2"})
_LOCK_POLL_SECONDS = 0.05
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_CONFIG_CHUNKS = 256
_PRIVATE_FILE_MODE = 0o600


class CamoufoxIdentityError(RuntimeError):
    """The persisted Camoufox identity is missing, corrupt, or incompatible."""


def _camoufox_version() -> str:
    try:
        return importlib.metadata.version("camoufox")
    except importlib.metadata.PackageNotFoundError as exc:
        raise CamoufoxIdentityError("Camoufox package metadata is unavailable") from exc


def _config_chunks(env: Any) -> list[str]:
    if not isinstance(env, dict):
        raise CamoufoxIdentityError(
            "Camoufox launch options did not contain an environment mapping"
        )

    numbered: list[tuple[int, str]] = []
    for key, value in env.items():
        if not isinstance(key, str) or not key.startswith("CAMOU_CONFIG_"):
            continue
        match = _CONFIG_ENV_RE.fullmatch(key)
        if match is None or not isinstance(value, str):
            raise CamoufoxIdentityError(
                "Camoufox generated malformed CAMOU_CONFIG chunks"
            )
        numbered.append((int(match.group(1)), value))

    numbered.sort(key=lambda item: item[0])
    if not numbered or len(numbered) > _MAX_CONFIG_CHUNKS:
        raise CamoufoxIdentityError(
            "Camoufox generated an invalid number of CAMOU_CONFIG chunks"
        )
    if [number for number, _ in numbered] != list(range(1, len(numbered) + 1)):
        raise CamoufoxIdentityError(
            "Camoufox generated non-contiguous CAMOU_CONFIG chunks"
        )
    return [value for _, value in numbered]


def _decode_config(chunks: list[str]) -> dict[str, Any]:
    raw = "".join(chunks)
    if not raw or len(raw.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise CamoufoxIdentityError("Camoufox identity config has an invalid size")
    try:
        config = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CamoufoxIdentityError(
            "Camoufox identity config is not valid JSON"
        ) from exc
    if not isinstance(config, dict):
        raise CamoufoxIdentityError("Camoufox identity config is not a JSON object")
    user_agent = config.get("navigator.userAgent")
    if not isinstance(user_agent, str) or "Firefox/" not in user_agent:
        raise CamoufoxIdentityError(
            "Camoufox identity config has no compatible Firefox user agent"
        )
    return config


def _is_dynamic_config_key(key: str) -> bool:
    return (
        key in _DYNAMIC_CONFIG_KEYS
        or key == "humanize"
        or key.startswith(_DYNAMIC_CONFIG_PREFIXES)
    )


def _identity_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value for key, value in config.items() if not _is_dynamic_config_key(key)
    }


def _fingerprint_platform(config: dict[str, Any]) -> dict[str, str]:
    """Return the claimed browser platform, not the physical host platform.

    Source login and isolated bridges may intentionally run on different hosts
    (for example a macOS host and a Linux Docker container). Camoufox applies
    the target platform encoded in CAMOU_CONFIG; binding to ``platform.machine``
    would reject a portable identity even when the generated browser identity
    and Camoufox build are identical.
    """
    signature: dict[str, str] = {}
    for key in ("navigator.platform", "navigator.oscpu"):
        value = config.get(key)
        if isinstance(value, str) and value:
            signature[key] = value
    if not signature:
        raise CamoufoxIdentityError("Camoufox identity has no browser platform")
    return signature


def _firefox_version(user_agent: str) -> str:
    """Return the browser build encoded in a Camoufox Firefox UA.

    BrowserForge legitimately varies non-build UA tokens between generated
    Linux fingerprints (for example, ``X11; Linux x86_64`` versus
    ``X11; Ubuntu; Linux x86_64``). Those tokens are part of the persisted
    identity and will be replaced by it; only the Firefox build must remain
    compatible with the installed Camoufox browser.
    """
    match = _FIREFOX_VERSION_RE.search(user_agent)
    if match is None:
        raise CamoufoxIdentityError(
            "Camoufox identity config has no compatible Firefox version"
        )
    return match.group(1)


def _encode_config(config: dict[str, Any]) -> list[str]:
    raw = json.dumps(
        config,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if not raw or len(raw.encode("utf-8")) > _MAX_CONFIG_BYTES:
        raise CamoufoxIdentityError("Camoufox identity config has an invalid size")
    # Match Camoufox's own get_env_vars() limits. The environment protocol is
    # ordered CAMOU_CONFIG_N chunks, so re-encoding a merged config is equivalent
    # to passing the original generated payload to the browser.
    chunk_size = 2047 if os.name == "nt" else 32767
    return [raw[index : index + chunk_size] for index in range(0, len(raw), chunk_size)]


def _identity_preferences(options: dict[str, Any]) -> dict[str, Any]:
    raw = options.get("firefox_user_prefs", {})
    if not isinstance(raw, dict):
        raise CamoufoxIdentityError(
            "Camoufox launch options had malformed Firefox preferences"
        )
    return {key: raw[key] for key in _IDENTITY_FIREFOX_PREFS if key in raw}


def _identity_digest(chunks: list[str], preferences: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"config_chunks": chunks, "firefox_preferences": preferences},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _new_artifact(options: dict[str, Any]) -> dict[str, Any]:
    config = _decode_config(_config_chunks(options.get("env")))
    identity_config = _identity_config(config)
    chunks = _encode_config(identity_config)
    preferences = _identity_preferences(options)
    return {
        "format": _ARTIFACT_FORMAT,
        "version": _ARTIFACT_VERSION,
        "camoufox_version": _camoufox_version(),
        "platform": _fingerprint_platform(identity_config),
        "firefox_user_agent": identity_config["navigator.userAgent"],
        "config_chunks": chunks,
        "firefox_preferences": preferences,
        "identity_sha256": _identity_digest(chunks, preferences),
    }


def _validated_artifact(
    payload: Any,
    *,
    current_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CamoufoxIdentityError("Camoufox identity artifact is not a JSON object")
    if payload.get("format") != _ARTIFACT_FORMAT:
        raise CamoufoxIdentityError("Unrecognized Camoufox identity artifact format")
    if payload.get("version") != _ARTIFACT_VERSION:
        raise CamoufoxIdentityError("Unsupported Camoufox identity artifact version")
    if payload.get("camoufox_version") != _camoufox_version():
        raise CamoufoxIdentityError(
            "Camoufox identity was created by an incompatible package version"
        )
    chunks = payload.get("config_chunks")
    if not isinstance(chunks, list) or not all(
        isinstance(chunk, str) for chunk in chunks
    ):
        raise CamoufoxIdentityError("Camoufox identity chunks are malformed")
    # Re-run the same structural/size validation used for newly generated
    # chunks without reconstructing a fake environment mapping.
    if not chunks or len(chunks) > _MAX_CONFIG_CHUNKS:
        raise CamoufoxIdentityError("Camoufox identity has an invalid chunk count")
    config = _decode_config(chunks)
    if any(_is_dynamic_config_key(key) for key in config):
        raise CamoufoxIdentityError(
            "Camoufox identity artifact contains dynamic runtime config"
        )
    if payload.get("platform") != _fingerprint_platform(config):
        raise CamoufoxIdentityError("Camoufox identity browser platform does not match")

    preferences = payload.get("firefox_preferences")
    if not isinstance(preferences, dict) or any(
        key not in _IDENTITY_FIREFOX_PREFS for key in preferences
    ):
        raise CamoufoxIdentityError(
            "Camoufox identity Firefox preferences are malformed"
        )
    expected_digest = _identity_digest(chunks, preferences)
    if payload.get("identity_sha256") != expected_digest:
        raise CamoufoxIdentityError("Camoufox identity checksum does not match")
    if payload.get("firefox_user_agent") != config["navigator.userAgent"]:
        raise CamoufoxIdentityError("Camoufox identity user agent does not match")

    if current_options is not None:
        current_config = _decode_config(_config_chunks(current_options.get("env")))
        if _firefox_version(current_config["navigator.userAgent"]) != _firefox_version(
            config["navigator.userAgent"]
        ) or _fingerprint_platform(current_config) != _fingerprint_platform(config):
            raise CamoufoxIdentityError(
                "Camoufox browser build is incompatible with the persisted identity"
            )

    return payload


def _load_artifact(
    path: Path,
    *,
    current_options: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CamoufoxIdentityError(
            f"Camoufox identity artifact is unreadable: {path}"
        ) from exc
    return _validated_artifact(payload, current_options=current_options)


def _try_acquire_lock(handle: BinaryIO) -> bool:
    if os.name == "nt":
        import msvcrt

        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _release_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def _identity_file_lock(path: Path) -> AsyncIterator[None]:
    """Serialize first-writer identity creation across tasks and processes."""
    lock_path = path.with_name(f".{path.name}.lock")
    secure_mkdir(lock_path.parent)
    harden_linkedin_tree(lock_path.parent)
    handle = lock_path.open("a+b")
    if os.name != "nt":
        lock_path.chmod(_PRIVATE_FILE_MODE)
    acquired = False
    try:
        while not acquired:
            acquired = _try_acquire_lock(handle)
            if not acquired:
                await asyncio.sleep(_LOCK_POLL_SECONDS)
        yield
    finally:
        if acquired:
            _release_lock(handle)
        handle.close()


def _apply_artifact(
    current_options: dict[str, Any], artifact: dict[str, Any]
) -> dict[str, Any]:
    prepared = {**current_options}
    current_env = current_options.get("env")
    if not isinstance(current_env, dict):
        raise CamoufoxIdentityError(
            "Camoufox launch options did not contain an environment mapping"
        )
    env = {
        key: value
        for key, value in current_env.items()
        if not (isinstance(key, str) and key.startswith("CAMOU_CONFIG_"))
    }
    current_config = _decode_config(_config_chunks(current_env))
    identity_config = _decode_config(artifact["config_chunks"])
    # Start from current *dynamic* policy only. BrowserForge can randomly add
    # or omit optional identity fields between launches (for example
    # navigator.globalPrivacyControl); overlaying the artifact on the whole
    # fresh fingerprint would therefore leak current-only fields and make the
    # effective identity drift even though its digest stayed unchanged.
    merged_config = {
        key: value
        for key, value in current_config.items()
        if _is_dynamic_config_key(key)
    }
    merged_config.update(identity_config)
    for index, chunk in enumerate(_encode_config(merged_config), start=1):
        env[f"CAMOU_CONFIG_{index}"] = chunk
    prepared["env"] = env

    raw_preferences = current_options.get("firefox_user_prefs", {})
    if not isinstance(raw_preferences, dict):
        raise CamoufoxIdentityError(
            "Camoufox launch options had malformed Firefox preferences"
        )
    preferences = {**raw_preferences, **artifact["firefox_preferences"]}
    prepared["firefox_user_prefs"] = preferences
    return prepared


async def prepare_camoufox_launch_options(
    current_options: dict[str, Any],
    identity_path: Path,
    *,
    expected_identity_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Apply one stable Camoufox identity to freshly generated options.

    ``current_options`` must be a new result from Camoufox's own
    ``launch_options()``.  The first caller atomically persists its identity;
    later callers reuse it while keeping every non-identity option current.
    Supplying ``expected_identity_sha256`` binds a source-session generation to
    its exact fingerprint and makes a missing or replaced artifact fail closed.
    """
    identity_path = identity_path.expanduser().resolve()
    generated = _new_artifact(current_options)
    async with _identity_file_lock(identity_path):
        if identity_path.exists():
            artifact = _load_artifact(
                identity_path,
                current_options=current_options,
            )
        else:
            if expected_identity_sha256 is not None:
                raise CamoufoxIdentityError(
                    "Camoufox identity required by the source session is missing"
                )
            artifact = generated
            secure_write_text(
                identity_path,
                json.dumps(artifact, indent=2, sort_keys=True) + "\n",
                mode=_PRIVATE_FILE_MODE,
            )
            harden_linkedin_tree(identity_path.parent)

    digest = artifact["identity_sha256"]
    if expected_identity_sha256 is not None and digest != expected_identity_sha256:
        raise CamoufoxIdentityError(
            "Camoufox identity does not match the source session generation"
        )
    return _apply_artifact(current_options, artifact), digest


def load_camoufox_identity_sha256(identity_path: Path) -> str:
    """Return a validated identity digest for source-state publication."""
    artifact = _load_artifact(identity_path.expanduser().resolve())
    return artifact["identity_sha256"]
