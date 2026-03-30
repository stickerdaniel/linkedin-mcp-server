"""Authentication state for persistent browser profiles."""

from __future__ import annotations

import json
import logging
import platform
import shutil
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any
from uuid import uuid4

from linkedin_mcp_server.common_utils import utcnow_iso
from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)
_SOURCE_STATE_FILE = "source-state.json"


@dataclass
class SourceState:
    version: int
    source_runtime_id: str
    login_generation: str
    created_at: str
    profile_path: str
    cookies_path: str


_SOURCE_STATE_FIELDS = frozenset(field.name for field in fields(SourceState))


def get_source_profile_dir() -> Path:
    return Path(get_config().browser.user_data_dir).expanduser()


def auth_root_dir(source_profile_dir: Path | None = None) -> Path:
    return (source_profile_dir or get_source_profile_dir()).expanduser().resolve().parent


def portable_cookie_path(source_profile_dir: Path | None = None) -> Path:
    return auth_root_dir(source_profile_dir) / "cookies.json"


def source_state_path(source_profile_dir: Path | None = None) -> Path:
    return auth_root_dir(source_profile_dir) / _SOURCE_STATE_FILE


def profile_exists(profile_dir: Path | None = None) -> bool:
    profile_dir = (profile_dir or get_source_profile_dir()).expanduser()
    return profile_dir.is_dir() and any(profile_dir.iterdir())


def get_runtime_id() -> str:
    os_name = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(
        platform.system(), platform.system().lower() or "unknown"
    )
    arch = platform.machine().lower()
    if arch in {"x86_64", "amd64"}:
        arch = "amd64"
    elif arch in {"arm64", "aarch64"}:
        arch = "arm64"
    return f"{os_name}-{arch}-host"


def load_source_state(source_profile_dir: Path | None = None) -> SourceState | None:
    data = _load_json(source_state_path(source_profile_dir))
    if not data:
        return None
    try:
        return SourceState(**{k: v for k, v in data.items() if k in _SOURCE_STATE_FIELDS})
    except TypeError:
        logger.warning("Ignoring invalid source-state.json")
        return None


def write_source_state(source_profile_dir: Path | None = None) -> SourceState:
    profile_dir = (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    state = SourceState(
        version=1,
        source_runtime_id=get_runtime_id(),
        login_generation=str(uuid4()),
        created_at=utcnow_iso(),
        profile_path=str(profile_dir),
        cookies_path=str(portable_cookie_path(profile_dir)),
    )
    _write_json(source_state_path(profile_dir), asdict(state))
    return state


def clear_auth_state(source_profile_dir: Path | None = None) -> bool:
    profile_dir = (source_profile_dir or get_source_profile_dir()).expanduser()
    targets = [
        profile_dir,
        portable_cookie_path(profile_dir),
        source_state_path(profile_dir),
    ]
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


_HW_CONCURRENCY_OPTIONS = (4, 8, 10, 12, 16)
_DEVICE_MEMORY_OPTIONS = (4, 8, 16, 32)


def fingerprint_path(source_profile_dir: Path | None = None) -> Path:
    """Path to the pinned browser fingerprint file."""
    return auth_root_dir(source_profile_dir) / "fingerprint.json"


def get_or_create_fingerprint(
    source_profile_dir: Path | None = None,
) -> dict[str, int]:
    """Load or generate a pinned browser fingerprint.

    Values are randomized once on first login, then reused across every
    browser launch so that login and server browsers present identical
    hardware signals to LinkedIn.
    """
    import random as _rng  # local to avoid top-level import for one callsite

    fp_file = fingerprint_path(source_profile_dir)
    if fp_file.exists():
        try:
            data = json.loads(fp_file.read_text())
            if "hardwareConcurrency" in data and "deviceMemory" in data:
                return data
        except (OSError, json.JSONDecodeError):
            logger.warning("Corrupt fingerprint file, regenerating: %s", fp_file)

    fp: dict[str, int] = {
        "hardwareConcurrency": _rng.choice(_HW_CONCURRENCY_OPTIONS),  # noqa: S311
        "deviceMemory": _rng.choice(_DEVICE_MEMORY_OPTIONS),  # noqa: S311
    }
    _write_json(fp_file, fp)
    logger.info("Pinned browser fingerprint created: %s", fp)
    return fp
