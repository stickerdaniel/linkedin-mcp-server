"""Runtime-aware authentication state for cross-platform profile reuse."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
import errno
import json
import logging
import os
import platform
from pathlib import Path
import shutil
import tempfile
from typing import Any, AsyncIterator, BinaryIO
from uuid import uuid4

from linkedin_mcp_server.common_utils import (
    harden_linkedin_tree,
    secure_mkdir,
    secure_write_text,
    utcnow_iso,
)
from linkedin_mcp_server.config import get_config

logger = logging.getLogger(__name__)

_SOURCE_STATE_FILE = "source-state.json"
_CAMOUFOX_IDENTITY_FILE = "camoufox-identity.json"
_RUNTIME_STATE_FILE = "runtime-state.json"
_RUNTIME_PROFILES_DIR = "runtime-profiles"
_INVALID_STATE_PREFIX = "invalid-state-"
_SOURCE_SESSION_LOCK_FILE = ".source-session.lock"
_SOURCE_TRANSACTION_PREFIX = ".source-session-transaction-"
_SOURCE_TRANSACTION_MANIFEST = "manifest.json"
_PENDING_PROFILE_OWNER_LOCK = ".owner.lock"
_SOURCE_LOCK_POLL_SECONDS = 0.1
_PRIVATE_AUTH_PATTERNS = (
    f"{_INVALID_STATE_PREFIX}*",
    ".source-session-rollback-*",
    f"{_SOURCE_TRANSACTION_PREFIX}*",
)
_PENDING_AUTH_PATTERNS = (".login-pending-*", ".import-pending-*")

_held_source_session_locks: ContextVar[tuple[tuple[str, asyncio.Task[Any]], ...]] = (
    ContextVar("held_source_session_locks", default=())
)

# A runtime ID identifies a compatible browser platform and is intentionally
# stable across processes. Browser profiles themselves require exclusive
# ownership, so each process gets a separate instance ID underneath that
# platform namespace. Tracking the PID alongside the cached value makes a
# forked child regenerate its inherited ID on first use.
_runtime_instance_pid: int | None = None
_runtime_instance_id: str | None = None

# When teardown is uncertain, the owning process keeps the pending profile's
# advisory lease open until process exit.  A concurrent --logout can then
# remove canonical/backup credentials while safely retaining only the profile
# that a browser might still own.  The OS releases every lease on process
# death, allowing a later logout to clean the abandoned directory.
_retained_pending_profile_leases: dict[str, BinaryIO] = {}


class PendingProfileLease:
    """Exclusive ownership marker for a login/import browser profile."""

    def __init__(self, root: Path, handle: BinaryIO):
        self.root = root
        self._handle: BinaryIO | None = handle
        self._retained = False

    @property
    def retained(self) -> bool:
        """Whether the process-lifetime registry now owns this lease."""
        return self._retained

    def release(self) -> None:
        """Release a confirmed-safe pending profile for normal deletion."""
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        _release_source_lock(handle)
        handle.close()

    def retain_until_exit(self) -> None:
        """Keep ownership when browser teardown could not be confirmed."""
        handle = self._handle
        if handle is None:
            return
        key = str(self.root)
        if key in _retained_pending_profile_leases:
            raise RuntimeError(
                f"Pending profile lease is already retained: {self.root}"
            )
        _retained_pending_profile_leases[key] = handle
        self._handle = None
        self._retained = True


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
    # Digest of the sanitized Camoufox fingerprint artifact used to mint this
    # source generation. None for Patchright and legacy source-state files.
    # Camoufox bridges must require an exact match rather than silently
    # generating a new fingerprint for an existing LinkedIn cookie.
    camoufox_identity_sha256: str | None = None


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


def camoufox_identity_path(source_profile_dir: Path | None = None) -> Path:
    """Return the sanitized persistent Camoufox fingerprint artifact path."""
    return auth_root_dir(source_profile_dir) / _CAMOUFOX_IDENTITY_FILE


def source_session_lock_path(source_profile_dir: Path | None = None) -> Path:
    """Return the advisory lock that serializes source-session mutation."""
    return auth_root_dir(source_profile_dir) / _SOURCE_SESSION_LOCK_FILE


def _try_acquire_source_lock(handle: BinaryIO) -> bool:
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


def _release_source_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def acquire_pending_profile_lease(pending_root: Path) -> PendingProfileLease:
    """Claim one unique login/import profile until teardown is known safe.

    Pending roots are UUID-namespaced and created while the caller owns the
    source-session lock, so contention here indicates a programming error. The
    separate lease remains useful after that source lock is released: logout
    can distinguish a dead owner's stale directory from a browser profile that
    may still be live in another process.
    """
    pending_root = pending_root.expanduser().resolve()
    secure_mkdir(pending_root)
    harden_linkedin_tree(pending_root)
    lock_path = pending_root / _PENDING_PROFILE_OWNER_LOCK
    handle = lock_path.open("a+b")
    if os.name != "nt":
        lock_path.chmod(0o600)
    try:
        if not _try_acquire_source_lock(handle):
            raise RuntimeError(f"Pending profile is already owned: {pending_root}")
    except BaseException:
        handle.close()
        raise
    return PendingProfileLease(pending_root, handle)


def _pending_profile_is_removable(pending_root: Path) -> bool:
    """Return whether no live process holds *pending_root*'s owner lease."""
    lock_path = pending_root / _PENDING_PROFILE_OWNER_LOCK
    if not lock_path.exists():
        # Compatibility for pending directories created before leases existed.
        # Current-version owners always create the marker before launching.
        return True
    try:
        handle = lock_path.open("r+b")
    except OSError as exc:
        logger.warning(
            "Could not inspect pending-profile owner lock %s: %s", lock_path, exc
        )
        return False
    acquired = False
    try:
        acquired = _try_acquire_source_lock(handle)
        return acquired
    except OSError as exc:
        logger.warning(
            "Could not test pending-profile owner lock %s: %s", lock_path, exc
        )
        return False
    finally:
        if acquired:
            _release_source_lock(handle)
        handle.close()


def reset_pending_profile_leases_for_testing() -> None:
    """Release process-retained pending leases between isolated tests."""
    retained = list(_retained_pending_profile_leases.values())
    _retained_pending_profile_leases.clear()
    for handle in retained:
        try:
            _release_source_lock(handle)
        finally:
            handle.close()


@asynccontextmanager
async def source_session_lock(
    source_profile_dir: Path | None = None,
    *,
    timeout_seconds: float | None = None,
    recover_transactions: bool = True,
) -> AsyncIterator[None]:
    """Serialize source-profile/cookie mutation across OS processes.

    The lock is advisory and owned by an open file descriptor, so the OS
    releases it automatically if a process exits. The lock file itself may
    remain and must not be treated as stale or deleted. Calls are reentrant
    within the same asyncio task, which lets high-level flows lock a complete
    transaction while lower-level helpers retain their own safety boundary.
    """
    path = source_session_lock_path(source_profile_dir)
    key = str(path.resolve())
    held = _held_source_session_locks.get()
    current_task = asyncio.current_task()
    if current_task is None:
        raise RuntimeError("source_session_lock requires an asyncio task")
    if any(held_key == key and owner is current_task for held_key, owner in held):
        yield
        return

    if timeout_seconds is not None and timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative or None")

    secure_mkdir(path.parent)
    harden_linkedin_tree(path.parent)
    handle = path.open("a+b")
    if os.name != "nt":
        path.chmod(0o600)

    acquired = False
    deadline = (
        None
        if timeout_seconds is None
        else asyncio.get_running_loop().time() + timeout_seconds
    )
    try:
        while not acquired:
            acquired = _try_acquire_source_lock(handle)
            if acquired:
                break
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"Timed out waiting for source-session lock: {path}")
            await asyncio.sleep(_SOURCE_LOCK_POLL_SECONDS)

        # A process may have died after atomically promoting only part of the
        # cookie + identity + state tuple. Recovery happens while holding the
        # same cross-process lock and before any cooperative reader can observe
        # the source generation.
        if recover_transactions:
            _recover_source_session_transactions(source_profile_dir)

        token = _held_source_session_locks.set((*held, (key, current_task)))
        try:
            yield
        finally:
            _held_source_session_locks.reset(token)
    finally:
        if acquired:
            _release_source_lock(handle)
        handle.close()


def portable_cookie_is_valid(source_profile_dir: Path | None = None) -> bool:
    """Return whether the portable snapshot has a usable LinkedIn session.

    File existence alone is misleading: an interrupted login can leave a
    structurally valid JSON file whose ``li_at`` value is empty.  Treat parse
    errors, wrong shapes, wrong domains, and blank values as unauthenticated.
    Cookie values are never logged.
    """
    path = portable_cookie_path(source_profile_dir)
    try:
        cookies = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(cookies, list):
        return False
    return any(
        isinstance(cookie, dict)
        and cookie.get("name") == "li_at"
        and (
            (domain := str(cookie.get("domain", "")).strip().lower().lstrip("."))
            == "linkedin.com"
            or domain.endswith(".linkedin.com")
        )
        and isinstance(cookie.get("value"), str)
        and bool(cookie["value"].strip())
        for cookie in cookies
    )


def source_state_path(source_profile_dir: Path | None = None) -> Path:
    """Return the source session metadata path."""
    return auth_root_dir(source_profile_dir) / _SOURCE_STATE_FILE


def runtime_profiles_root(source_profile_dir: Path | None = None) -> Path:
    """Return the root directory for derived runtime profiles."""
    return auth_root_dir(source_profile_dir) / _RUNTIME_PROFILES_DIR


def get_runtime_instance_id() -> str:
    """Return a process-unique identity for an active browser profile.

    The value is cached for the process lifetime. A forked child inherits the
    module globals, so the cached PID is checked on every call and a new UUID is
    generated whenever the current PID changes.
    """
    global _runtime_instance_id, _runtime_instance_pid

    current_pid = os.getpid()
    if _runtime_instance_id is None or _runtime_instance_pid != current_pid:
        _runtime_instance_pid = current_pid
        _runtime_instance_id = f"{current_pid}-{uuid4().hex}"
    return _runtime_instance_id


def rotate_runtime_instance_id() -> str:
    """Abandon the current profile namespace and return a fresh instance ID.

    Call this when browser teardown was not confirmed or a transport failure
    leaves profile ownership uncertain. The old directory is intentionally
    preserved; a later attempt must never delete/reopen it under a possibly
    live browser process.
    """
    global _runtime_instance_id, _runtime_instance_pid

    _runtime_instance_pid = os.getpid()
    _runtime_instance_id = f"{_runtime_instance_pid}-{uuid4().hex}"
    return _runtime_instance_id


def runtime_dir(runtime_id: str, source_profile_dir: Path | None = None) -> Path:
    """Return this process's derived-session directory for one runtime."""
    return runtime_instance_dir(
        runtime_id,
        get_runtime_instance_id(),
        source_profile_dir,
    )


def runtime_instance_dir(
    runtime_id: str,
    instance_id: str,
    source_profile_dir: Path | None = None,
) -> Path:
    """Return an explicitly captured PID+UUID runtime instance directory."""
    if Path(instance_id).name != instance_id or instance_id in {"", ".", ".."}:
        raise ValueError(f"Invalid runtime instance ID: {instance_id!r}")
    return (
        runtime_profiles_root(source_profile_dir)
        / runtime_id
        / "instances"
        / instance_id
    )


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


def stage_bound_camoufox_identity(
    staged_identity_path: Path,
    source_profile_dir: Path | None = None,
) -> str | None:
    """Copy the canonical Camoufox identity only when source-state binds it.

    Login and browser-import recovery must never open or overwrite the
    canonical identity directly.  A corrupt, incompatible, or unbound artifact
    is therefore treated as unusable and the caller is left with no staged
    file, which lets Camoufox generate a fresh pending identity.  Callers hold
    :func:`source_session_lock`, so the source-state/artifact pair cannot change
    between validation and copying by another cooperative process.

    Returns the validated bound digest when an identity was seeded, otherwise
    ``None``.
    """
    from linkedin_mcp_server.core.camoufox_identity import (
        CamoufoxIdentityError,
        load_camoufox_identity_sha256,
    )

    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    canonical_identity_path = camoufox_identity_path(profile_dir)
    staged_identity_path = staged_identity_path.expanduser().resolve()
    if staged_identity_path == canonical_identity_path:
        raise ValueError("staged Camoufox identity must differ from the canonical path")

    staged_identity_path.unlink(missing_ok=True)
    source_state = load_source_state(profile_dir)
    expected_digest = (
        source_state.camoufox_identity_sha256 if source_state is not None else None
    )
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        return None

    try:
        canonical_digest = load_camoufox_identity_sha256(canonical_identity_path)
    except (CamoufoxIdentityError, OSError) as exc:
        logger.warning(
            "Ignoring unusable canonical Camoufox identity while staging a new "
            "source session: %s",
            exc,
        )
        return None
    if canonical_digest != expected_digest:
        logger.warning("Ignoring canonical Camoufox identity not bound to source-state")
        return None

    try:
        secure_mkdir(staged_identity_path.parent)
        shutil.copy2(canonical_identity_path, staged_identity_path)
        if os.name != "nt":
            staged_identity_path.chmod(0o600)
        harden_linkedin_tree(staged_identity_path.parent)
        staged_digest = load_camoufox_identity_sha256(staged_identity_path)
    except (CamoufoxIdentityError, OSError) as exc:
        staged_identity_path.unlink(missing_ok=True)
        logger.warning("Could not stage the bound Camoufox identity: %s", exc)
        return None
    if staged_digest != expected_digest:
        staged_identity_path.unlink(missing_ok=True)
        logger.warning("Staged Camoufox identity changed while it was being copied")
        return None
    return staged_digest


def write_source_state(
    source_profile_dir: Path | None = None,
    *,
    user_agent: str | None = None,
    camoufox_identity_sha256: str | None = None,
) -> SourceState:
    """Write a fresh source session generation after successful login."""
    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    state = SourceState(
        version=2,
        source_runtime_id=get_runtime_id(),
        login_generation=str(uuid4()),
        created_at=utcnow_iso(),
        profile_path=str(profile_dir),
        cookies_path=str(portable_cookie_path(profile_dir)),
        user_agent=user_agent,
        camoufox_identity_sha256=camoufox_identity_sha256,
    )
    _write_json(source_state_path(profile_dir), asdict(state))
    return state


def commit_source_session(
    staged_cookie_path: Path,
    source_profile_dir: Path | None = None,
    *,
    user_agent: str | None = None,
    staged_camoufox_identity_path: Path | None = None,
    camoufox_identity_sha256: str | None = None,
) -> SourceState:
    """Publish cookies, Camoufox identity, and metadata as one transaction.

    Callers must hold :func:`source_session_lock` for the whole operation. The
    staged identity is validated before any canonical artifact changes and its
    digest is derived here rather than trusted from a caller.  A commit without
    a staged identity explicitly publishes a non-Camoufox generation and
    removes any previous canonical identity. If any promotion or state write
    fails, all three canonical artifacts are restored byte-for-byte to their
    pre-commit state (or removed when they did not previously exist).

    A filesystem cannot atomically replace two independent paths. Keeping
    private rollback copies inside the auth root makes the observable failure
    mode safe for cooperative readers, which also hold the source-session lock.
    """
    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    staged_cookie_path = staged_cookie_path.expanduser().resolve()
    cookie_path = portable_cookie_path(profile_dir)
    state_path = source_state_path(profile_dir)
    identity_path = camoufox_identity_path(profile_dir)
    if staged_cookie_path == cookie_path:
        raise ValueError("staged cookie path must differ from canonical cookies.json")
    if not staged_cookie_path.is_file():
        raise FileNotFoundError(staged_cookie_path)

    staged_identity: Path | None = None
    validated_identity_digest: str | None = None
    if staged_camoufox_identity_path is not None:
        from linkedin_mcp_server.core.camoufox_identity import (
            load_camoufox_identity_sha256,
        )

        staged_identity = staged_camoufox_identity_path.expanduser().resolve()
        if staged_identity == identity_path:
            raise ValueError(
                "staged Camoufox identity must differ from the canonical path"
            )
        if staged_identity == staged_cookie_path:
            raise ValueError("staged cookie and Camoufox identity paths must differ")
        if not staged_identity.is_file():
            raise FileNotFoundError(staged_identity)
        # Full schema, version, checksum, platform, UA, and digest validation.
        validated_identity_digest = load_camoufox_identity_sha256(staged_identity)
        if (
            camoufox_identity_sha256 is not None
            and camoufox_identity_sha256 != validated_identity_digest
        ):
            raise ValueError(
                "provided Camoufox identity digest does not match staged artifact"
            )
    elif camoufox_identity_sha256 is not None:
        raise ValueError(
            "Camoufox identity digest requires a staged Camoufox identity artifact"
        )

    root = auth_root_dir(profile_dir)
    secure_mkdir(root)
    transaction_dir = Path(
        tempfile.mkdtemp(prefix=_SOURCE_TRANSACTION_PREFIX, dir=root)
    )
    if os.name != "nt":
        transaction_dir.chmod(0o700)
    cookie_backup = transaction_dir / "cookies.json"
    state_backup = transaction_dir / _SOURCE_STATE_FILE
    identity_backup = transaction_dir / _CAMOUFOX_IDENTITY_FILE
    cookie_existed = cookie_path.is_file()
    state_existed = state_path.is_file()
    identity_existed = identity_path.is_file()
    preserve_rollback = False
    try:
        if cookie_existed:
            shutil.copy2(cookie_path, cookie_backup)
        if state_existed:
            shutil.copy2(state_path, state_backup)
        if identity_existed:
            shutil.copy2(identity_path, identity_backup)
        # The manifest is the journal's commit-preparation marker. It is
        # written atomically only after every required rollback copy exists and
        # before the first canonical artifact changes. If the process is killed
        # after this point, the next source-lock owner restores the old tuple.
        _write_json(
            transaction_dir / _SOURCE_TRANSACTION_MANIFEST,
            {
                "version": 1,
                "profile_path": str(profile_dir),
                "artifacts": {
                    "cookies": cookie_existed,
                    "source_state": state_existed,
                    "camoufox_identity": identity_existed,
                },
            },
        )
        _fsync_paths(
            [
                path
                for path in (
                    cookie_backup,
                    state_backup,
                    identity_backup,
                    transaction_dir / _SOURCE_TRANSACTION_MANIFEST,
                )
                if path.exists()
            ],
            directory=transaction_dir,
        )
        try:
            staged_cookie_path.replace(cookie_path)
            if staged_identity is not None:
                staged_identity.replace(identity_path)
            else:
                identity_path.unlink(missing_ok=True)
            source_state = write_source_state(
                profile_dir,
                user_agent=user_agent,
                camoufox_identity_sha256=validated_identity_digest,
            )
            _fsync_paths(
                [
                    path
                    for path in (cookie_path, state_path, identity_path)
                    if path.exists()
                ],
                directory=root,
            )
            # Removing the prepared marker is the single atomic commit point.
            # Before it, recovery restores the old tuple; after it, all new
            # artifacts are durable and an orphaned directory is just debris.
            (transaction_dir / _SOURCE_TRANSACTION_MANIFEST).unlink()
            try:
                _fsync_directory(transaction_dir)
            except OSError as exc:
                # The canonical tuple is already complete and the atomic
                # marker removal is visible. Do not attempt a rollback that no
                # longer has a prepared marker; retain a diagnostic warning.
                logger.warning(
                    "Could not fsync committed source transaction directory %s: %s",
                    transaction_dir,
                    exc,
                )
            return source_state
        except BaseException as commit_error:
            try:
                _rollback_source_transaction(transaction_dir, profile_dir)
            except Exception as rollback_error:
                preserve_rollback = True
                logger.critical(
                    "Source-session commit and rollback both failed; private "
                    "recovery copies were preserved at %s",
                    transaction_dir,
                    exc_info=commit_error,
                )
                raise RuntimeError(
                    "Source-session commit failed and rollback was incomplete; "
                    f"recovery copies were preserved at {transaction_dir}"
                ) from rollback_error
            raise
    finally:
        staged_cookie_path.unlink(missing_ok=True)
        if staged_identity is not None:
            staged_identity.unlink(missing_ok=True)
        if not preserve_rollback:
            shutil.rmtree(transaction_dir, ignore_errors=True)


def _transaction_manifest(
    transaction_dir: Path, source_profile_dir: Path
) -> dict[str, bool]:
    """Load and validate one prepared source-session transaction journal."""
    manifest_path = transaction_dir / _SOURCE_TRANSACTION_MANIFEST
    try:
        payload = json.loads(manifest_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"Unreadable source-session transaction journal: {transaction_dir}"
        ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Invalid source-session transaction journal: {transaction_dir}"
        )
    artifacts = payload.get("artifacts")
    expected_keys = {"cookies", "source_state", "camoufox_identity"}
    if not (
        payload.get("version") == 1
        and payload.get("profile_path") == str(source_profile_dir.resolve())
        and isinstance(artifacts, dict)
        and set(artifacts) == expected_keys
        and all(isinstance(artifacts[key], bool) for key in expected_keys)
    ):
        raise RuntimeError(
            f"Invalid source-session transaction journal: {transaction_dir}"
        )
    return artifacts


def _rollback_source_transaction(
    transaction_dir: Path, source_profile_dir: Path
) -> None:
    """Restore the pre-commit tuple recorded in a prepared transaction."""
    artifacts = _transaction_manifest(transaction_dir, source_profile_dir)
    for path, backup, key in (
        (
            source_state_path(source_profile_dir),
            transaction_dir / _SOURCE_STATE_FILE,
            "source_state",
        ),
        (
            camoufox_identity_path(source_profile_dir),
            transaction_dir / _CAMOUFOX_IDENTITY_FILE,
            "camoufox_identity",
        ),
        (
            portable_cookie_path(source_profile_dir),
            transaction_dir / "cookies.json",
            "cookies",
        ),
    ):
        _restore_source_artifact(path, backup, artifacts[key])
    _fsync_paths(
        [
            path
            for path in (
                portable_cookie_path(source_profile_dir),
                source_state_path(source_profile_dir),
                camoufox_identity_path(source_profile_dir),
            )
            if path.exists()
        ],
        directory=auth_root_dir(source_profile_dir),
    )


def _recover_source_session_transactions(
    source_profile_dir: Path | None = None,
) -> None:
    """Rollback journals left by a killed source-session publisher.

    This function must run only while holding the source-session advisory lock.
    A directory without a manifest was interrupted before canonical mutation
    began and is safe to discard. A malformed prepared journal blocks access
    rather than guessing which credential generation is valid.
    """
    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    root = auth_root_dir(profile_dir)
    if not root.exists():
        return
    for transaction_dir in sorted(root.glob(f"{_SOURCE_TRANSACTION_PREFIX}*")):
        if transaction_dir.is_symlink() or not transaction_dir.is_dir():
            raise RuntimeError(
                f"Invalid source-session transaction path: {transaction_dir}"
            )
        manifest_path = transaction_dir / _SOURCE_TRANSACTION_MANIFEST
        if not manifest_path.exists():
            shutil.rmtree(transaction_dir)
            continue
        try:
            _rollback_source_transaction(transaction_dir, profile_dir)
            shutil.rmtree(transaction_dir)
        except Exception as exc:
            logger.critical(
                "Could not recover interrupted source-session transaction at %s",
                transaction_dir,
                exc_info=True,
            )
            raise RuntimeError(
                "Interrupted source-session publication could not be recovered; "
                f"private recovery copies remain at {transaction_dir}"
            ) from exc
        logger.warning(
            "Recovered source session after interrupted publication at %s",
            transaction_dir,
        )


def _fsync_directory(path: Path) -> None:
    """Durably order journal/rename operations on POSIX filesystems."""
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_paths(paths: list[Path], *, directory: Path) -> None:
    """Flush files followed by their containing directory on POSIX."""
    if os.name == "nt":
        return
    for path in paths:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    _fsync_directory(directory)


def _restore_source_artifact(path: Path, backup: Path, existed: bool) -> None:
    """Restore one auth artifact captured by :func:`commit_source_session`."""
    if existed:
        restore_path = backup.with_name(f".{backup.name}.restore-{uuid4().hex}")
        shutil.copy2(backup, restore_path)
        restore_path.replace(path)
    else:
        path.unlink(missing_ok=True)


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
    """Remove only this process's derived runtime profile and metadata."""
    target = runtime_dir(runtime_id, source_profile_dir)
    if not target.exists():
        return True
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        logger.warning("Could not clear runtime profile %s: %s", target, exc)
        return False


async def clear_auth_state(
    source_profile_dir: Path | None = None,
    *,
    timeout_seconds: float | None = 30,
) -> bool:
    """Remove source credentials without deleting another process's profile.

    Canonical and pending/backup artifacts are serialized with login/import
    publication through the source-session lock. Process-isolated runtime
    profiles are deliberately retained: another MCP server may still own a
    browser lock there, and deleting the global tree cannot log that browser
    out safely. Each owner removes only its own PID+UUID instance after a
    confirmed teardown.
    """
    profile_dir = (
        (source_profile_dir or get_source_profile_dir()).expanduser().resolve()
    )
    async with source_session_lock(
        profile_dir,
        timeout_seconds=timeout_seconds,
        # Logout destroys prepared/recovery journals themselves. Skipping
        # recovery here prevents a malformed journal from blocking secure
        # credential deletion or briefly republishing its backup first.
        recover_transactions=False,
    ):
        root = auth_root_dir(profile_dir)
        targets = [
            profile_dir,
            portable_cookie_path(profile_dir),
            source_state_path(profile_dir),
            camoufox_identity_path(profile_dir),
        ]
        for pattern in _PRIVATE_AUTH_PATTERNS:
            if root.exists():
                targets.extend(root.glob(pattern))
        pending_targets: list[Path] = []
        for pattern in _PENDING_AUTH_PATTERNS:
            if root.exists():
                pending_targets.extend(root.glob(pattern))

        success = True
        seen: set[Path] = set()
        for target in [*targets, *pending_targets]:
            if target in seen or not target.exists():
                continue
            seen.add(target)
            if (
                target in pending_targets
                and target.is_dir()
                and not target.is_symlink()
                and not _pending_profile_is_removable(target)
            ):
                logger.warning(
                    "Retaining pending authentication profile still owned by "
                    "another live process: %s",
                    target,
                )
                success = False
                continue
            try:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            except OSError as exc:
                logger.warning("Could not clear auth artifact %s: %s", target, exc)
                success = False

        runtime_root = runtime_profiles_root(profile_dir)
        if runtime_root.exists():
            logger.warning(
                "Retaining process-isolated runtime profiles during logout; "
                "their browser owners must tear them down independently: %s",
                runtime_root,
            )
    return success


def clear_runtime_instance(
    runtime_id: str,
    instance_id: str,
    source_profile_dir: Path | None = None,
) -> bool:
    """Remove one captured instance after its browser teardown was confirmed."""
    target = runtime_instance_dir(runtime_id, instance_id, source_profile_dir)
    if not target.exists():
        return True
    try:
        shutil.rmtree(target)
        return True
    except OSError as exc:
        logger.warning("Could not clear runtime instance %s: %s", target, exc)
        return False


_MAX_INVALID_STATE_BACKUPS = 5


def move_artifacts_aside(
    targets: list[Path], source_profile_dir: Path | None = None
) -> Path | None:
    """Move existing *targets* into one fresh timestamped backup directory.

    Skips targets that don't exist. Returns the backup directory, or
    ``None`` when none of the targets existed (nothing to back up).

    Used wherever a "this profile might be invalid" heuristic used to call
    ``shutil.rmtree``/``unlink`` directly: backing up instead of destroying
    means a misclassified failure (e.g. a transport error mistaken for a
    real session rejection) never permanently loses real session state. The
    caller is responsible for any additional guard (e.g. bootstrap.py checks
    ``_auth_ready()`` first) -- this function unconditionally backs up
    whatever it's given.

    Keeps only the most recent ``_MAX_INVALID_STATE_BACKUPS`` backup
    directories (pruning older ones on each call) so a host that hits this
    path repeatedly doesn't accumulate backups without bound.
    """
    existing = [target for target in targets if target.exists()]
    if not existing:
        return None

    root = auth_root_dir(source_profile_dir)
    backup_dir = root / (
        f"{_INVALID_STATE_PREFIX}{utcnow_iso().replace(':', '-')}-{uuid4().hex}"
    )
    secure_mkdir(backup_dir)
    for target in existing:
        shutil.move(str(target), str(backup_dir / target.name))

    _prune_old_backups(root)
    return backup_dir


def _prune_old_backups(root: Path) -> None:
    """Delete all but the most recent _MAX_INVALID_STATE_BACKUPS backup dirs.

    ISO-8601 timestamps in the directory name sort lexicographically in
    chronological order, so a plain name sort is enough -- no need to stat
    each directory's mtime.
    """
    backups = sorted(
        (p for p in root.glob(f"{_INVALID_STATE_PREFIX}*") if p.is_dir()),
        reverse=True,
    )
    for stale in backups[_MAX_INVALID_STATE_BACKUPS:]:
        try:
            shutil.rmtree(stale)
        except OSError as exc:
            logger.warning("Could not prune old backup %s: %s", stale, exc)


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
