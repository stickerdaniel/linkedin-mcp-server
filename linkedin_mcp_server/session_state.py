"""Runtime-aware authentication state for cross-platform profile reuse."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
import logging
import platform
from pathlib import Path
import shutil
from typing import Any
from uuid import uuid4

from linkedin_mcp_server.common_utils import (
    secure_mkdir,
    secure_write_text,
    utcnow_iso,
)
from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)

_SOURCE_STATE_FILE = "source-state.json"
_RUNTIME_STATE_FILE = "runtime-state.json"
_RUNTIME_PROFILES_DIR = "runtime-profiles"

# Prefix of the timestamped directories retired auth state is moved into.
QUARANTINE_PREFIX = "invalid-state-"

# Chromium writes these into a profile it currently owns and removes them on a
# clean exit. Their presence means another process — a second CLI run, a server,
# or a container sharing the mounted auth root — is using the profile right now.
_CHROMIUM_LOCK_NAMES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


@dataclass
class SourceState:
    version: int
    source_runtime_id: str
    login_generation: str
    created_at: str
    profile_path: str
    cookies_path: str
    # The user agent the session's cookies were minted under (synthesized from
    # the source browser during import, see browser_import/user_agent.py). None
    # for manual logins (the cookie is minted in the runtime browser itself, so
    # its default UA already matches) and for pre-existing state files.
    user_agent: str | None = None


@dataclass
class RuntimeState:
    version: int
    runtime_id: str
    source_runtime_id: str
    source_login_generation: str
    created_at: str
    committed_at: str
    profile_path: str
    storage_state_path: str
    commit_method: str


_SOURCE_STATE_FIELDS = frozenset(field.name for field in fields(SourceState))
_RUNTIME_STATE_FIELDS = frozenset(field.name for field in fields(RuntimeState))


def get_source_profile_dir() -> Path:
    """Return the configured source profile directory."""
    return Path(get_config().browser.user_data_dir).expanduser()


def auth_root_dir(source_profile_dir: Path | None = None) -> Path:
    """Return the root directory containing auth artifacts."""
    profile_dir = source_profile_dir or get_source_profile_dir()
    return profile_dir.expanduser().resolve().parent


def portable_cookie_path(source_profile_dir: Path | None = None) -> Path:
    """Return the portable cookie export path."""
    return auth_root_dir(source_profile_dir) / "cookies.json"


def source_state_path(source_profile_dir: Path | None = None) -> Path:
    """Return the source session metadata path."""
    return auth_root_dir(source_profile_dir) / _SOURCE_STATE_FILE


def runtime_profiles_root(source_profile_dir: Path | None = None) -> Path:
    """Return the root directory for derived runtime profiles."""
    return auth_root_dir(source_profile_dir) / _RUNTIME_PROFILES_DIR


def runtime_dir(runtime_id: str, source_profile_dir: Path | None = None) -> Path:
    """Return the directory for one runtime's derived session."""
    return runtime_profiles_root(source_profile_dir) / runtime_id


def runtime_profile_dir(
    runtime_id: str, source_profile_dir: Path | None = None
) -> Path:
    """Return the profile directory for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / "profile"


def runtime_state_path(runtime_id: str, source_profile_dir: Path | None = None) -> Path:
    """Return the metadata path for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / _RUNTIME_STATE_FILE


def runtime_storage_state_path(
    runtime_id: str, source_profile_dir: Path | None = None
) -> Path:
    """Return the storage-state snapshot path for one runtime's derived session."""
    return runtime_dir(runtime_id, source_profile_dir) / "storage-state.json"


def profile_exists(profile_dir: Path | None = None) -> bool:
    """Check if a browser profile directory exists and is non-empty."""
    profile_dir = (profile_dir or get_source_profile_dir()).expanduser()
    return profile_dir.is_dir() and any(profile_dir.iterdir())


def get_runtime_id() -> str:
    """Return a deterministic identity for the current browser runtime."""
    os_name = _normalize_os(platform.system())
    arch = _normalize_arch(platform.machine())
    runtime_kind = "container" if _is_container_runtime() else "host"
    return f"{os_name}-{arch}-{runtime_kind}"


def _normalize_os(system: str) -> str:
    mapping = {
        "Darwin": "macos",
        "Linux": "linux",
        "Windows": "windows",
    }
    return mapping.get(system, system.lower() or "unknown")


def _normalize_arch(machine: str) -> str:
    value = machine.lower()
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value or "unknown"


def _is_container_runtime() -> bool:
    if any(
        path.exists()
        for path in (
            Path("/.dockerenv"),
            Path("/run/.containerenv"),
            Path("/run/containerenv"),
        )
    ):
        return True

    markers = ("docker", "containerd", "kubepods", "podman", "libpod")
    for probe in (
        Path("/proc/1/cgroup"),
        Path("/proc/self/cgroup"),
    ):
        if _path_contains_markers(probe, markers):
            return True

    for probe in (
        Path("/proc/1/mountinfo"),
        Path("/proc/self/mountinfo"),
    ):
        if _path_contains_markers(probe, markers) or _root_mount_uses_overlay(probe):
            return True

    return False


def _path_contains_markers(path: Path, markers: tuple[str, ...]) -> bool:
    if not path.exists():
        return False

    try:
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False

    return any(marker in text for marker in markers)


def _root_mount_uses_overlay(path: Path) -> bool:
    if not path.exists():
        return False

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False

    for line in lines:
        if " - " not in line:
            continue
        left, right = line.split(" - ", maxsplit=1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        if left_fields[4] == "/" and right_fields[0] == "overlay":
            return True

    return False


def load_source_state(source_profile_dir: Path | None = None) -> SourceState | None:
    """Load the source session metadata if present."""
    data = _load_json(source_state_path(source_profile_dir))
    if not data:
        return None
    try:
        return SourceState(
            **{key: value for key, value in data.items() if key in _SOURCE_STATE_FIELDS}
        )
    except TypeError:
        logger.warning("Ignoring invalid source-state.json")
        return None


def write_source_state(
    source_profile_dir: Path | None = None,
    *,
    user_agent: str | None = None,
) -> SourceState:
    """Write a fresh source session generation after successful login."""
    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    state = SourceState(
        version=1,
        source_runtime_id=get_runtime_id(),
        login_generation=str(uuid4()),
        created_at=utcnow_iso(),
        profile_path=str(profile_dir),
        cookies_path=str(portable_cookie_path(profile_dir)),
        user_agent=user_agent,
    )
    _write_json(source_state_path(profile_dir), asdict(state))
    return state


def load_runtime_state(
    runtime_id: str, source_profile_dir: Path | None = None
) -> RuntimeState | None:
    """Load one derived runtime's metadata if present."""
    data = _load_json(runtime_state_path(runtime_id, source_profile_dir))
    if not data:
        return None
    try:
        return RuntimeState(
            **{
                key: value
                for key, value in data.items()
                if key in _RUNTIME_STATE_FIELDS
            }
        )
    except TypeError:
        logger.warning("Ignoring invalid runtime-state.json for %s", runtime_id)
        return None


def write_runtime_state(
    runtime_id: str,
    source_state: SourceState,
    storage_state_path: Path,
    source_profile_dir: Path | None = None,
    *,
    created_at: str | None = None,
    commit_method: str = "checkpoint_restart",
) -> RuntimeState:
    """Write metadata for a derived runtime session."""
    profile_dir = runtime_profile_dir(runtime_id, source_profile_dir).resolve()
    committed_at = utcnow_iso()
    state = RuntimeState(
        version=1,
        runtime_id=runtime_id,
        source_runtime_id=source_state.source_runtime_id,
        source_login_generation=source_state.login_generation,
        created_at=created_at or committed_at,
        committed_at=committed_at,
        profile_path=str(profile_dir),
        storage_state_path=str(storage_state_path.resolve()),
        commit_method=commit_method,
    )
    _write_json(runtime_state_path(runtime_id, source_profile_dir), asdict(state))
    return state


def clear_runtime_profile(
    runtime_id: str, source_profile_dir: Path | None = None
) -> bool:
    """Remove one derived runtime profile and its metadata."""
    target = runtime_dir(runtime_id, source_profile_dir)
    if not target.exists():
        return True
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        logger.warning("Could not clear runtime profile %s: %s", target, exc)
        return False


def _auth_state_targets(profile_dir: Path) -> list[Path]:
    """The four artifacts that together make up one source session."""
    return [
        profile_dir,
        portable_cookie_path(profile_dir),
        source_state_path(profile_dir),
        runtime_profiles_root(profile_dir),
    ]


def quarantine_dirs(source_profile_dir: Path | None = None) -> list[Path]:
    """Existing quarantine directories, newest name last."""
    root = auth_root_dir(source_profile_dir)
    if not root.is_dir():
        return []
    return sorted(path for path in root.glob(f"{QUARANTINE_PREFIX}*") if path.is_dir())


def profile_in_use_by(profile_dir: Path) -> Path | None:
    """The Chromium lock file proving another process owns *profile_dir*.

    Returns ``None`` when the profile is free. The lock is a symlink on Linux
    and macOS and may dangle, so existence is probed via ``lstat`` rather than
    ``exists()``, which follows the link and reports False for a stale target.
    """
    for name in _CHROMIUM_LOCK_NAMES:
        candidate = profile_dir / name
        try:
            candidate.lstat()
        except OSError:
            continue
        return candidate
    return None


def rotate_source_profile(source_profile_dir: Path | None = None) -> Path | None:
    """Retire the current source session so the next one starts clean.

    Chromium mints ``machine_id``, ``session_id_generator_last_value`` and
    friends into ``Local State`` once and then keeps them for the life of the
    profile. Reusing the directory for a different LinkedIn account hands
    LinkedIn the same device identity twice, which is exactly the signal that
    links accounts to one another. Every path that establishes a new source
    session therefore rotates first.

    The retired artifacts are moved, not deleted, so a session that turns out to
    have been fine is still recoverable. ``--logout`` clears the quarantines.

    Returns the quarantine directory, or ``None`` when there was nothing to
    retire.

    Raises:
        RuntimeError: Another process holds the profile. Rotating underneath a
            live Chromium corrupts both the old and the new session.
        OSError: A move failed. Deliberately not swallowed: a half-rotated tree
            would leave the next rotation splitting one session across two
            quarantines.
    """
    profile_dir = (source_profile_dir or get_source_profile_dir()).expanduser()
    existing = [
        target for target in _auth_state_targets(profile_dir) if target.exists()
    ]
    if not existing:
        return None

    lock = profile_in_use_by(profile_dir)
    if lock is not None:
        raise RuntimeError(
            f"The browser profile is in use by another process (found {lock.name}). "
            "Stop the running server or container before creating a new session."
        )

    # utcnow_iso() is second-resolution and rotation is now routine rather than
    # exceptional, so two rotations can land in the same second. The suffix keeps
    # them from merging into one directory.
    stamp = utcnow_iso().replace(":", "-")
    backup_dir = (
        auth_root_dir(profile_dir) / f"{QUARANTINE_PREFIX}{stamp}-{uuid4().hex[:8]}"
    )
    secure_mkdir(backup_dir)
    for target in existing:
        shutil.move(str(target), str(backup_dir / target.name))
    logger.info("Retired previous session to %s", backup_dir)
    return backup_dir


def clear_auth_state(source_profile_dir: Path | None = None) -> bool:
    """Remove source auth artifacts, derived runtime profiles and quarantines."""
    profile_dir = (source_profile_dir or get_source_profile_dir()).expanduser()
    # Quarantines hold previous sessions' cookies, so a logout that left them
    # behind would not be the "clear all stored auth state" the CLI advertises.
    targets = _auth_state_targets(profile_dir) + quarantine_dirs(profile_dir)

    success = True
    for target in targets:
        if not target.exists():
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            logger.warning("Could not clear auth artifact %s: %s", target, exc)
            success = False
    return success


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring unreadable auth state file: %s", path)
        return None
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed auth state file: %s", path)
        return None
    return data


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    secure_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
