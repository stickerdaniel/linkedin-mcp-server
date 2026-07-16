"""Managed runtime bootstrap for browser setup and LinkedIn login."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from enum import Enum
import errno
import functools
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import Any, AsyncIterator, BinaryIO, Callable, NoReturn

from fastmcp import Context

from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text, utcnow_iso
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.drivers.browser import (
    close_browser,
    current_headless,
    get_profile_dir,
    set_headless,
)
from linkedin_mcp_server.exceptions import (
    AuthenticationBootstrapFailedError,
    AuthenticationInProgressError,
    AuthenticationStartedError,
    BrowserSetupFailedError,
    BrowserSetupInProgressError,
    DockerHostLoginRequiredError,
)
from linkedin_mcp_server.session_state import (
    auth_root_dir,
    camoufox_identity_path,
    get_runtime_id,
    load_source_state,
    move_artifacts_aside,
    portable_cookie_path,
    portable_cookie_is_valid,
    source_session_lock,
    source_state_path,
)
from linkedin_mcp_server.setup import interactive_login

logger = logging.getLogger(__name__)

_BROWSER_DIR = "patchright-browsers"
_BROWSER_INSTALL_METADATA = "browser-install.json"
_INSTALL_METADATA_SCHEMA = 3
_CAMOUFOX_INSTALL_LOCK_FILE = ".camoufox-install.lock"
_CAMOUFOX_READY_MARKER_FILE = ".linkedin-mcp-ready.json"
_CAMOUFOX_READY_MARKER_SCHEMA = 1
_CAMOUFOX_INSTALL_LOCK_POLL_SECONDS = 0.1
_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS = 5
_CAMOUFOX_FETCH_TIMEOUT_SECONDS = 600
_CAMOUFOX_FETCH_FAILURE_MARKERS = (
    "failed to download and extract",
    "error installing camoufox",
)


@dataclass(slots=True)
class _CamoufoxInstallLease:
    """Ownership metadata for a cache lock that may outlive its caller."""

    process: Any | None = None
    reap_task: asyncio.Task[Any] | None = None
    cleanup: Callable[[], object] | None = None

    def retain_until_reaped(
        self,
        process: Any,
        reap_task: asyncio.Task[Any],
    ) -> None:
        self.process = process
        self.reap_task = reap_task

    def defer_cleanup(self, cleanup: Callable[[], object]) -> None:
        self.cleanup = cleanup

    @property
    def must_retain(self) -> bool:
        return self.process is not None and self.reap_task is not None


# A cancelled fetch normally terminates and reaps its child before returning.
# In the hard-failure case (an unkillable child or an OS signalling error), keep
# the advisory-lock file descriptor alive until wait()+communicate() have
# completed and the process has a return code. This prevents a later fetch or
# another process from mutating the same Camoufox cache concurrently.
_retained_camoufox_install_locks: dict[
    Path, tuple[BinaryIO, _CamoufoxInstallLease]
] = {}

# Registry browser names mapped to on-disk dir prefixes for the binaries this
# server actually launches. ffmpeg/firefox/webkit are excluded — ffmpeg is only
# used for video recording (we don't), and chromium / chromium-headless-shell
# entries have no revisionOverrides, so we avoid patchright's per-platform
# special-prefix logic entirely.
_REGISTRY_NAME_TO_DIR_PREFIX = {
    "chromium": "chromium-",
    "chromium-headless-shell": "chromium_headless_shell-",
}

# On-disk dir prefix of the headless shell — the only binary the default
# headless scrape + auto-import path launches.
_SHELL_DIR_PREFIX = "chromium_headless_shell-"
# On-disk dir prefix of full Chrome for Testing — needed only for the headed
# interactive-login fallback or an operator-configured --no-headless run.
_FULL_DIR_PREFIX = "chromium-"


class RuntimePolicy(str, Enum):
    MANAGED = "managed"
    DOCKER = "docker"


class SetupState(str, Enum):
    IDLE = "not_started"
    RUNNING = "installing"
    READY = "ready"
    FAILED = "failed"


class AuthState(str, Enum):
    IDLE = "idle"
    STARTING = "starting_login"
    IN_PROGRESS = "login_in_progress"
    READY = "auth_ready"
    FAILED = "failed"


@dataclass(slots=True)
class BootstrapState:
    runtime_policy: RuntimePolicy | None = None
    setup_state: SetupState = SetupState.IDLE
    auth_state: AuthState = AuthState.IDLE
    last_error: str | None = None
    setup_started_at: str | None = None
    setup_completed_at: str | None = None
    auth_started_at: str | None = None
    auth_completed_at: str | None = None
    setup_task: asyncio.Task[None] | None = None
    login_task: asyncio.Task[None] | None = None
    import_task: asyncio.Task[bool] | None = None
    import_attempted: bool = False
    force_auth_reset: bool = False
    force_auth_reset_generation: str | None = None
    initialized: bool = False


_state = BootstrapState()
_lock = asyncio.Lock()


def reset_bootstrap_for_testing() -> None:
    """Reset bootstrap singleton state for test isolation."""
    global _state, _lock, _AUTO_IMPORT_ANNOUNCED
    for task in (_state.setup_task, _state.login_task, _state.import_task):
        if task is not None and not task.done():
            task.cancel()
    _state = BootstrapState()
    _lock = asyncio.Lock()
    _AUTO_IMPORT_ANNOUNCED = False
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    # Tolerate monkeypatched stand-ins that lack `cache_clear`.
    clear = getattr(_patchright_install_targets, "cache_clear", None)
    if clear is not None:
        clear()


def get_runtime_policy() -> RuntimePolicy:
    """Return the active bootstrap runtime policy."""
    if _state.runtime_policy is not None:
        return _state.runtime_policy
    return (
        RuntimePolicy.DOCKER
        if get_runtime_id().endswith("-container")
        else RuntimePolicy.MANAGED
    )


def browsers_path() -> Path:
    """Return the shared user-level Patchright browser cache path."""
    return auth_root_dir(get_profile_dir()) / _BROWSER_DIR


def install_metadata_path() -> Path:
    """Return the browser install metadata path."""
    return auth_root_dir(get_profile_dir()) / _BROWSER_INSTALL_METADATA


def configure_browser_environment() -> Path:
    """Ensure the shared browser cache path is configured and return the effective path.

    Honors a pre-set ``PLAYWRIGHT_BROWSERS_PATH`` so install metadata and
    readiness checks operate on the same path patchright actually uses.
    The path is normalized (``~`` expanded, made absolute) and written back
    to the env var so metadata writes, readiness checks, and patchright
    subprocesses all agree on the same string.
    """
    raw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(browsers_path())
    normalized = Path(raw).expanduser().absolute()
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(normalized)
    return normalized


def _patchright_pkg_version() -> str | None:
    try:
        return importlib.metadata.version("patchright")
    except importlib.metadata.PackageNotFoundError:
        return None


@functools.cache
def _patchright_install_targets() -> dict[str, str] | None:
    """Resolve {dir_prefix: revision} from patchright's bundled browsers.json.

    Reads ``<patchright>/driver/package/browsers.json`` — the authoritative
    file patchright itself consults to know which revision it expects.
    Returns ``None`` if the registry can't be read; callers treat ``None``
    as "not ready" so the next gate triggers reinstall.

    Cached for the process lifetime: the patchright revision only changes on
    package upgrade, which requires a process restart. Tests reset the cache
    via ``reset_bootstrap_for_testing()``.
    """
    try:
        import patchright

        registry = (
            Path(patchright.__file__).parent / "driver" / "package" / "browsers.json"
        )
        payload = json.loads(registry.read_text())
    except (ImportError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None

    targets: dict[str, str] = {}
    for entry in payload.get("browsers", []):
        if not isinstance(entry, dict) or not entry.get("installByDefault"):
            continue
        prefix = _REGISTRY_NAME_TO_DIR_PREFIX.get(entry.get("name"))
        if prefix is None or entry.get("revision") is None:
            continue
        targets[prefix] = str(entry["revision"])
    return targets or None


def _has_install_for(configured: Path, prefix: str, revision: str) -> bool:
    return (configured / f"{prefix}{revision}" / "INSTALLATION_COMPLETE").is_file()


def _uses_custom_chrome() -> bool:
    """Return whether an operator-supplied Chrome/Chromium executable is set.

    Every launch passes ``executable_path`` from ``chrome_path``, so the managed
    Patchright binary is never used and its background install is unnecessary.
    ``chrome_path`` is a Chromium-only option and must not bypass Camoufox's own
    binary readiness gate.
    """
    browser = get_config().browser
    engine = getattr(browser, "browser_engine", "patchright")
    return engine == "patchright" and bool(browser.chrome_path)


def _engine_self_manages_binary() -> bool:
    """Return whether the configured engine uses a non-Patchright provisioner.

    Delegates to the engine's own adapter (``core.engines.ENGINES``) rather
    than naming a specific engine here, so a future third engine that
    uses its own binary (like Camoufox, cached under ``~/.cache/camoufox``)
    does not accidentally run Patchright's Chromium installer. Such engines
    still need their own explicit readiness and provisioning path below.
    """
    from linkedin_mcp_server.core.engines import ENGINES

    # getattr guards test doubles/fakes that predate this field and don't set it.
    engine = getattr(get_config().browser, "browser_engine", "patchright")
    return not ENGINES[engine].needs_managed_install(None)


def _skips_managed_binary() -> bool:
    """Return whether Patchright's managed-binary install should be skipped.

    True for either a custom Chrome/Chromium executable or an engine that
    uses its own provisioner. This says nothing about whether the selected
    engine is ready: Camoufox is checked and fetched separately.
    """
    return _uses_custom_chrome() or _engine_self_manages_binary()


def _camoufox_browser_dir() -> Path | None:
    """Return the installed, supported Camoufox browser root without mutation.

    Camoufox 0.4's ``camoufox_path(download_if_missing=False)`` is read-only,
    but probing through package metadata avoids depending on that side effect.
    The dependency is intentionally capped below 0.5 because 0.5 changes the
    browser, addon, and GeoIP layouts together.
    """
    try:
        from camoufox.pkgman import INSTALL_DIR, Version

        install_dir = Path(INSTALL_DIR)
        return install_dir if Version.from_path(install_dir).is_supported() else None
    except Exception:
        logger.debug("Camoufox browser binary is not ready", exc_info=True)
        return None


def _camoufox_binary_path() -> Path | None:
    """Return the supported Camoufox executable path without mutation."""
    try:
        from camoufox.pkgman import LAUNCH_FILE, OS_NAME

        browser_dir = _camoufox_browser_dir()
        if browser_dir is None:
            return None
        launch_file = Path(LAUNCH_FILE[OS_NAME])
        if OS_NAME == "mac":
            return browser_dir / "Camoufox.app" / "Contents" / "Resources" / launch_file
        return browser_dir / launch_file
    except Exception:
        logger.debug("Camoufox browser binary is not ready", exc_info=True)
        return None


def _camoufox_resource_dir(browser_dir: Path) -> Path:
    from camoufox.pkgman import OS_NAME

    if OS_NAME == "mac":
        return browser_dir / "Camoufox.app" / "Contents" / "Resources"
    return browser_dir


def _camoufox_addon_dirs(browser_dir: Path) -> list[Path]:
    from camoufox.addons import DefaultAddons

    addon_root = _camoufox_resource_dir(browser_dir) / "addons"
    return [addon_root / addon.name for addon in DefaultAddons]


def _camoufox_addon_ready(addon_dir: Path) -> bool:
    manifest_path = addon_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(manifest, dict) and all(
        manifest.get(key) for key in ("manifest_version", "name", "version")
    )


def _camoufox_mmdb_path() -> Path | None:
    try:
        from camoufox.locale import ALLOW_GEOIP, MMDB_FILE

        return Path(MMDB_FILE) if ALLOW_GEOIP else None
    except Exception:
        return None


def _camoufox_mmdb_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        from geoip2.database import Reader

        with Reader(str(path)):
            pass
        return True
    except Exception:
        return False


def _camoufox_assets_ready() -> bool:
    """Validate every non-browser asset used by our Camoufox adapter."""
    browser_dir = _camoufox_browser_dir()
    mmdb_path = _camoufox_mmdb_path()
    return (
        browser_dir is not None
        and mmdb_path is not None
        and _camoufox_mmdb_ready(mmdb_path)
        and all(
            _camoufox_addon_ready(path) for path in _camoufox_addon_dirs(browser_dir)
        )
    )


def _camoufox_components_ready() -> bool:
    """Return whether the browser and launch-time assets are structurally usable."""
    executable = _camoufox_binary_path()
    if not _camoufox_executable_ready(executable):
        return False
    return _camoufox_assets_ready()


def _camoufox_executable_ready(executable: Path | None) -> bool:
    """Reject absent, empty and non-executable Camoufox launch binaries."""
    if executable is None or not executable.is_file():
        return False
    try:
        if executable.stat().st_size <= 0:
            return False
    except OSError:
        return False
    return os.name == "nt" or os.access(executable, os.X_OK)


def _camoufox_ready_marker_path() -> Path | None:
    try:
        from camoufox.pkgman import INSTALL_DIR

        return Path(INSTALL_DIR).expanduser() / _CAMOUFOX_READY_MARKER_FILE
    except Exception:
        return None


def _camoufox_ready_marker_valid() -> bool:
    marker_path = _camoufox_ready_marker_path()
    if marker_path is None:
        return False
    try:
        payload = json.loads(marker_path.read_text())
        package_version = importlib.metadata.version("camoufox")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        importlib.metadata.PackageNotFoundError,
    ):
        return False
    return payload == {
        "schema": _CAMOUFOX_READY_MARKER_SCHEMA,
        "camoufox_package_version": package_version,
    }


def _fsync_directory(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(path, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _invalidate_camoufox_ready_marker() -> None:
    """Make every lock-free readiness probe fail before cache mutation."""
    marker_path = _camoufox_ready_marker_path()
    if marker_path is None:
        raise BrowserSetupFailedError("Could not resolve Camoufox readiness marker")
    existed = marker_path.exists() or marker_path.is_symlink()
    try:
        marker_path.unlink(missing_ok=True)
        if marker_path.exists() or marker_path.is_symlink():
            raise OSError("marker still exists after unlink")
        if existed:
            _fsync_directory(marker_path.parent)
    except OSError as exc:
        raise BrowserSetupFailedError(
            f"Could not invalidate Camoufox readiness marker: {exc}"
        ) from exc


def _write_camoufox_ready_marker() -> None:
    """Publish the crash/cross-process completion proof as the final fetch step."""
    marker_path = _camoufox_ready_marker_path()
    if marker_path is None:
        raise BrowserSetupFailedError("Could not resolve Camoufox readiness marker")
    payload = {
        "schema": _CAMOUFOX_READY_MARKER_SCHEMA,
        "camoufox_package_version": importlib.metadata.version("camoufox"),
    }
    secure_mkdir(marker_path.parent)
    marker_fd, temp_name = tempfile.mkstemp(
        dir=marker_path.parent,
        prefix=f".{marker_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(marker_fd, "w") as marker_file:
            marker_file.write(json.dumps(payload, sort_keys=True) + "\n")
            marker_file.flush()
            os.fsync(marker_file.fileno())
        if os.name != "nt":
            temp_path.chmod(0o600)
        os.replace(temp_path, marker_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    # Once replace succeeds, the marker names fully-written, fsynced content
    # and is safe for readers. A filesystem that rejects directory fsync may
    # lose the marker after a crash (safe false-negative/refetch), but must not
    # turn a currently valid install into an exception after publication.
    try:
        _fsync_directory(marker_path.parent)
    except OSError as exc:
        logger.warning("Could not fsync Camoufox marker directory: %s", exc)


def camoufox_ready() -> bool:
    """Return whether a completed Camoufox install is safe to launch."""
    # Marker first: while another process is fetching, no consumer observes a
    # momentarily plausible binary/MMDB/manifest and launches into a partial
    # cache. The marker is removed before mutation and published last.
    return _camoufox_ready_marker_valid() and _camoufox_components_ready()


def _remove_incomplete_camoufox_assets() -> None:
    """Delete partial MMDB/addon outputs so Camoufox fetch will retry them."""
    mmdb_path = _camoufox_mmdb_path()
    if (
        mmdb_path is not None
        and mmdb_path.exists()
        and not _camoufox_mmdb_ready(mmdb_path)
    ):
        mmdb_path.unlink()

    browser_dir = _camoufox_browser_dir()
    if browser_dir is None:
        return
    for addon_dir in _camoufox_addon_dirs(browser_dir):
        if addon_dir.exists() and not _camoufox_addon_ready(addon_dir):
            if addon_dir.is_dir() and not addon_dir.is_symlink():
                shutil.rmtree(addon_dir)
            else:
                addon_dir.unlink()


def _remove_path_without_following_symlinks(path: Path) -> bool:
    """Best-effort removal for fetch outputs while the install lock is owned."""
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove incomplete Camoufox asset %s: %s", path, exc)
        return False
    return not path.exists() and not path.is_symlink()


def _remove_camoufox_fetch_assets() -> bool:
    """Unconditionally discard assets a failed/interrupted fetch may truncate."""
    removed = True
    try:
        mmdb_path = _camoufox_mmdb_path()
    except Exception as exc:
        logger.warning("Could not locate Camoufox GeoIP asset for cleanup: %s", exc)
        mmdb_path = None
    if mmdb_path is not None:
        removed = _remove_path_without_following_symlinks(mmdb_path) and removed
    else:
        removed = False

    browser_dir = _camoufox_browser_dir()
    if browser_dir is None:
        try:
            from camoufox.pkgman import INSTALL_DIR

            browser_dir = Path(INSTALL_DIR)
        except Exception:
            return False
    try:
        addon_dirs = _camoufox_addon_dirs(browser_dir)
    except Exception as exc:
        logger.warning("Could not locate Camoufox addons for cleanup: %s", exc)
        return False
    for addon_dir in addon_dirs:
        removed = _remove_path_without_following_symlinks(addon_dir) and removed
    return removed


def _remove_broken_camoufox_browser_cache(browser_dir: Path) -> bool:
    """Invalidate 0.4 cache metadata when its advertised binary is unusable."""
    try:
        from camoufox.pkgman import INSTALL_DIR

        install_dir = Path(INSTALL_DIR).expanduser()
        if browser_dir.expanduser().resolve() != install_dir.resolve():
            logger.error(
                "Refusing to remove unexpected Camoufox cache path %s", browser_dir
            )
            return False
        _remove_path_without_following_symlinks(install_dir)
        return not install_dir.exists()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("Could not invalidate broken Camoufox cache: %s", exc)
        return False


def _repair_camoufox_execute_permissions() -> bool:
    """Finish Camoufox's interrupted post-extraction chmod on POSIX.

    Camoufox 0.4 writes supported version metadata before its final recursive
    chmod. If the fetch process is killed in that narrow window, a later fetch
    reports "up to date" and never repairs the non-executable tree. Add only
    owner read/execute (and owner write for directories), preserving all other
    mode bits and never following symlinks.
    """
    if os.name == "nt":
        return False
    try:
        from camoufox.pkgman import INSTALL_DIR

        install_dir = Path(INSTALL_DIR)
        if not install_dir.is_dir():
            return False

        def repair_path(path: Path) -> None:
            if path.is_symlink():
                return
            mode = stat.S_IMODE(path.stat().st_mode) | stat.S_IRUSR | stat.S_IXUSR
            if path.is_dir():
                mode |= stat.S_IWUSR
            path.chmod(mode)

        repair_path(install_dir)
        for path in install_dir.rglob("*"):
            repair_path(path)
        return True
    except OSError as exc:
        logger.warning("Could not repair Camoufox cache permissions: %s", exc)
        return False


def _camoufox_install_lock_path() -> Path:
    """Return a lock beside (not inside) Camoufox's destructively replaced cache."""
    from camoufox.pkgman import INSTALL_DIR

    return Path(INSTALL_DIR).expanduser().resolve().parent / _CAMOUFOX_INSTALL_LOCK_FILE


def _try_acquire_install_lock(handle: BinaryIO) -> bool:
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


def _release_install_lock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def _camoufox_install_lock(
    lock_path: Path | None = None,
) -> AsyncIterator[_CamoufoxInstallLease]:
    """Serialize Camoufox fetch across every process sharing its user cache."""
    path = (lock_path or _camoufox_install_lock_path()).expanduser().resolve()
    secure_mkdir(path.parent)
    handle = path.open("a+b")
    if os.name != "nt":
        path.chmod(0o600)
    acquired = False
    lease = _CamoufoxInstallLease()
    try:
        while not acquired:
            if path not in _retained_camoufox_install_locks:
                acquired = _try_acquire_install_lock(handle)
            if not acquired:
                await asyncio.sleep(_CAMOUFOX_INSTALL_LOCK_POLL_SECONDS)
        yield lease
    finally:
        if acquired and lease.must_retain:
            assert lease.process is not None
            assert lease.reap_task is not None
            if (
                lease.reap_task.done()
                and getattr(lease.process, "returncode", None) is not None
            ):
                if lease.cleanup is not None:
                    lease.cleanup()
            else:
                _retain_camoufox_install_lock(path, handle, lease)
                acquired = False
        if acquired:
            try:
                _release_install_lock(handle)
            finally:
                handle.close()
        elif not lease.must_retain:
            handle.close()


def _retain_camoufox_install_lock(
    path: Path,
    handle: BinaryIO,
    lease: _CamoufoxInstallLease,
) -> None:
    """Transfer a lock to a child-completion callback after caller cancellation."""
    assert lease.process is not None
    assert lease.reap_task is not None
    _retained_camoufox_install_locks[path] = (handle, lease)

    def release_if_reaped(_task: asyncio.Task[Any]) -> None:
        retained = _retained_camoufox_install_locks.get(path)
        if retained is None or retained[0] is not handle:
            return
        if getattr(lease.process, "returncode", None) is None:
            logger.error(
                "Camoufox installer communicate() ended without reaping the child; "
                "retaining cache lock until process exit"
            )
            return
        try:
            with suppress(Exception, asyncio.CancelledError):
                _task.result()
            if lease.cleanup is not None:
                lease.cleanup()
            _release_install_lock(handle)
        except Exception as exc:
            logger.warning(
                "Could not explicitly release Camoufox install lock: %s", exc
            )
        finally:
            try:
                handle.close()
            except OSError as exc:
                logger.warning("Could not close Camoufox install lock: %s", exc)
            _retained_camoufox_install_locks.pop(path, None)

    lease.reap_task.add_done_callback(release_if_reaped)


def initialize_bootstrap(runtime_policy: RuntimePolicy | str | None = None) -> None:
    """Initialize bootstrap state and configure the shared browser cache."""
    if _state.initialized:
        return
    configure_browser_environment()
    _state.runtime_policy = RuntimePolicy(runtime_policy or get_runtime_policy())
    _state.initialized = True


def get_bootstrap_state() -> BootstrapState:
    """Return current bootstrap state."""
    return _state


async def start_background_browser_setup_if_needed() -> None:
    """Start shared background browser setup for managed runtimes if needed."""
    initialize_bootstrap()
    if get_runtime_policy() != RuntimePolicy.MANAGED:
        return
    if _uses_custom_chrome():
        # An explicit Patchright executable is provisioned by the operator.
        _state.setup_state = SetupState.READY
        _state.setup_completed_at = _state.setup_completed_at or utcnow_iso()
        return

    async with _lock:
        if _browser_setup_ready():
            _state.setup_state = SetupState.READY
            _state.setup_completed_at = _state.setup_completed_at or utcnow_iso()
            return
        if _state.setup_state == SetupState.READY:
            invalidate_browser_setup()
        if _state.setup_task is not None and not _state.setup_task.done():
            return
        _start_browser_setup_task_locked()


def _metadata_shape_ok() -> Path | None:
    """Validate the install metadata shape and return the configured browsers path.

    Returns the configured ``PLAYWRIGHT_BROWSERS_PATH`` when the metadata
    blob is present, current-schema, and self-consistent; ``None`` otherwise.
    The per-binary completion check is left to the caller so a shell-only
    install can be distinguished from a fully-provisioned one. Pure: no
    mutation of metadata or in-memory state.
    """
    metadata_path = install_metadata_path()
    configured_browsers_path = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path()))
    )
    if not metadata_path.exists() or not configured_browsers_path.exists():
        return None
    try:
        payload = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not (
        isinstance(payload, dict)
        and payload.get("browser_name") == "chromium"
        and payload.get("installer_name") == "patchright"
        and payload.get("version") == _INSTALL_METADATA_SCHEMA
    ):
        return None
    if payload.get("browsers_path") != str(configured_browsers_path):
        return None
    if payload.get("patchright_version") != _patchright_pkg_version():
        return None
    return configured_browsers_path


def shell_ready() -> bool:
    """Return whether the headless-shell binary is installed and current.

    The default headless scrape + auto-import path launches only the headless
    shell, so this is the readiness signal that gates a headless-mode server.
    Pure: no mutation.
    """
    configured = _metadata_shape_ok()
    if configured is None:
        return False
    targets = _patchright_install_targets()
    if not targets:
        return False
    revision = targets.get(_SHELL_DIR_PREFIX)
    if revision is None:
        return False
    return _has_install_for(configured, _SHELL_DIR_PREFIX, revision)


def full_chromium_ready() -> bool:
    """Return whether every chromium binary is installed and current.

    Requires both the full Chrome for Testing and the headless shell. This is
    the readiness signal that gates a headed (``--no-headless``) server and the
    interactive-login fallback. Pure: no mutation.
    """
    configured = _metadata_shape_ok()
    if configured is None:
        return False
    targets = _patchright_install_targets()
    if not targets:
        return False
    for prefix, revision in targets.items():
        if not _has_install_for(configured, prefix, revision):
            return False
    return True


def browser_setup_ready() -> bool:
    """Return whether the install required for the configured launch mode is current.

    Mode-aware: a headless-mode server needs only the headless shell; a headed
    (``--no-headless``) server needs full chromium. Pure: no mutation of
    metadata or in-memory state. Mutation happens in
    :func:`invalidate_browser_setup`, called by the gate paths.
    """
    if _engine_self_manages_binary():
        return camoufox_ready()
    if get_config().browser.headless:
        return shell_ready()
    return full_chromium_ready()


def invalidate_browser_setup() -> None:
    """Mark browser setup as not-ready: drop install metadata and reset cached READY state."""
    install_metadata_path().unlink(missing_ok=True)
    if _state.setup_state == SetupState.READY:
        _state.setup_state = SetupState.IDLE
        _state.setup_completed_at = None


def _browser_setup_ready() -> bool:
    """Compatibility wrapper for tests and internal callers."""
    return browser_setup_ready()


def _start_browser_setup_task_locked() -> None:
    _state.setup_state = SetupState.RUNNING
    _state.setup_started_at = utcnow_iso()
    _state.last_error = None
    _state.setup_completed_at = None
    _state.setup_task = asyncio.create_task(_run_browser_setup(), name="browser-setup")


async def _run_patchright_install(extra_arg: str) -> None:
    """Run one ``patchright install chromium`` stage with the given flag.

    The patchright registry lock serializes concurrent installs, so the two
    stages always run one after the other on the same browsers path.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "patchright",
        "install",
        "chromium",
        extra_arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate_installer(proc)
    if proc.returncode != 0:
        output = "\n".join(
            text for text in (stderr.decode().strip(), stdout.decode().strip()) if text
        )
        raise BrowserSetupFailedError(
            output or "Patchright Chromium browser setup failed."
        )


async def _terminate_and_reap_subprocess(
    proc: Any,
    communicate_task: asyncio.Task[tuple[bytes, bytes]],
) -> tuple[bool, asyncio.Task[Any]]:
    """Stop a cancelled installer child and always collect its exit status."""

    async def confirm_reaped() -> None:
        # communicate() itself can fail before asyncio's child watcher has set
        # returncode. A separate wait() supplies the later completion edge the
        # retained-lock callback needs; reusing an already-done communicate
        # task would otherwise retain the lock until process exit.
        wait = getattr(proc, "wait", None)
        if callable(wait):
            with suppress(Exception, asyncio.CancelledError):
                await wait()
        with suppress(Exception, asyncio.CancelledError):
            await communicate_task

    reap_task = asyncio.create_task(confirm_reaped())

    async def wait_bounded(timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while not reap_task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(asyncio.shield(reap_task), timeout=remaining)
            except asyncio.CancelledError:
                # A repeated cancellation must not strand the installer child.
                # Cleanup stays bounded by the original monotonic deadline.
                continue
            except TimeoutError:
                return (
                    reap_task.done() and getattr(proc, "returncode", None) is not None
                )
            except Exception:
                break
        with suppress(Exception, asyncio.CancelledError):
            reap_task.result()
        return reap_task.done() and getattr(proc, "returncode", None) is not None

    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("Could not terminate cancelled installer child: %s", exc)
    if await wait_bounded(_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS):
        return True, reap_task
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.warning("Could not kill cancelled installer child: %s", exc)
    if not await wait_bounded(_SUBPROCESS_TERMINATE_TIMEOUT_SECONDS):
        logger.error("Timed out reaping cancelled browser installer subprocess")
        return False, reap_task
    return True, reap_task


async def _communicate_installer(
    proc: Any,
    on_unreaped: Callable[[Any, asyncio.Task[Any]], None] | None = None,
) -> tuple[bytes, bytes]:
    communicate_task = asyncio.create_task(proc.communicate())
    try:
        return await asyncio.shield(communicate_task)
    except (Exception, asyncio.CancelledError):
        reaped, reap_task = await _terminate_and_reap_subprocess(proc, communicate_task)
        if not reaped and on_unreaped is not None:
            on_unreaped(proc, reap_task)
        raise


async def _run_camoufox_fetch() -> None:
    """Fetch Camoufox through its supported CLI and verify the executable.

    The post-check is required because some Camoufox CLI versions print a
    download error and still exit successfully. A zero subprocess exit status
    alone must therefore never advance the bootstrap state to READY.
    """
    async with _camoufox_install_lock() as install_lease:
        # A second process may have completed the install while this caller was
        # waiting. Re-check only after owning the cache-wide advisory lock.
        try:
            from camoufox.pkgman import INSTALL_DIR

            install_dir = Path(INSTALL_DIR).expanduser()
        except Exception:
            install_dir = None
        browser_dir = _camoufox_browser_dir()
        candidate = _camoufox_binary_path()
        if (
            candidate is not None
            and candidate.is_file()
            and os.name != "nt"
            and not os.access(candidate, os.X_OK)
        ):
            _repair_camoufox_execute_permissions()
            candidate = _camoufox_binary_path()
        if (
            install_dir is not None
            and install_dir.exists()
            and (browser_dir is None or not _camoufox_executable_ready(candidate))
        ):
            if not _remove_broken_camoufox_browser_cache(install_dir):
                raise BrowserSetupFailedError(
                    "Could not invalidate unusable Camoufox browser cache"
                )
        _remove_incomplete_camoufox_assets()
        if camoufox_ready():
            return
        # A missing/invalid marker means the preceding fetch may have died at
        # any extraction offset. Invalidate first so lock-free consumers fail,
        # then remove every upstream asset whose downloader skips on existence.
        _invalidate_camoufox_ready_marker()
        if not _remove_camoufox_fetch_assets():
            raise BrowserSetupFailedError(
                "Could not clear incomplete Camoufox runtime assets before fetch"
            )
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "camoufox",
            "fetch",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with asyncio.timeout(_CAMOUFOX_FETCH_TIMEOUT_SECONDS):
                stdout, stderr = await _communicate_installer(
                    proc, on_unreaped=install_lease.retain_until_reaped
                )
        except TimeoutError as exc:
            if install_lease.must_retain:
                install_lease.defer_cleanup(_remove_camoufox_fetch_assets)
            else:
                _remove_camoufox_fetch_assets()
            raise BrowserSetupFailedError(
                "Camoufox browser setup timed out after "
                f"{_CAMOUFOX_FETCH_TIMEOUT_SECONDS} seconds"
            ) from exc
        except (Exception, asyncio.CancelledError):
            if install_lease.must_retain:
                install_lease.defer_cleanup(_remove_camoufox_fetch_assets)
            else:
                _remove_camoufox_fetch_assets()
            raise
        if proc.returncode != 0:
            _remove_camoufox_fetch_assets()
            output = "\n".join(
                text
                for text in (
                    stderr.decode(errors="replace").strip(),
                    stdout.decode(errors="replace").strip(),
                )
                if text
            )
            raise BrowserSetupFailedError(output or "Camoufox browser setup failed.")
        combined_output = "\n".join(
            text
            for text in (
                stderr.decode(errors="replace").strip(),
                stdout.decode(errors="replace").strip(),
            )
            if text
        )
        if any(
            marker in combined_output.casefold()
            for marker in _CAMOUFOX_FETCH_FAILURE_MARKERS
        ):
            _remove_camoufox_fetch_assets()
            raise BrowserSetupFailedError(
                combined_output or "Camoufox reported an incomplete runtime download."
            )
        if not _camoufox_components_ready():
            _remove_camoufox_fetch_assets()
            raise BrowserSetupFailedError(
                "Camoufox fetch completed without installing usable runtime components."
            )
        _write_camoufox_ready_marker()
        if not camoufox_ready():
            _invalidate_camoufox_ready_marker()
            _remove_camoufox_fetch_assets()
            raise BrowserSetupFailedError(
                "Camoufox fetch completion marker could not be verified."
            )


def _write_install_metadata(
    browser_dir: Path, installed_targets: dict[str, bool]
) -> None:
    """Record the install state, including which binaries are present on disk."""
    metadata = {
        "version": _INSTALL_METADATA_SCHEMA,
        "runtime_id": get_runtime_id(),
        "installed_at": utcnow_iso(),
        "browsers_path": str(browser_dir),
        "browser_name": "chromium",
        "installer_name": "patchright",
        "patchright_version": _patchright_pkg_version(),
        "installed_targets": installed_targets,
    }
    secure_write_text(
        install_metadata_path(),
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    )


def _needs_full_chromium() -> bool:
    """Return whether the full-chromium stage should run during background setup.

    The shell alone covers the default headless scrape + auto-import path. Full
    chromium is installed up front only for a headed (``--no-headless``) run or
    when the operator opts into pre-warming the headed login fallback.
    """
    config = get_config()
    return (not config.browser.headless) or config.browser.eager_full_chromium


async def _run_browser_setup() -> None:
    """Install the configured engine's browser runtime.

    Camoufox has one browser artifact for headed and headless launches, fetched
    through its own module. Patchright remains two-stage: the headless shell is
    installed first and full Chrome for Testing only when the configured mode
    needs it.
    """
    if _engine_self_manages_binary():
        await _run_camoufox_fetch()
        return

    browser_dir = configure_browser_environment()
    secure_mkdir(browser_dir)

    await _run_patchright_install("--only-shell")
    _write_install_metadata(
        browser_dir,
        {_SHELL_DIR_PREFIX: True, _FULL_DIR_PREFIX: False},
    )

    if _needs_full_chromium():
        await _run_patchright_install("--no-shell")
        _write_install_metadata(
            browser_dir,
            {_SHELL_DIR_PREFIX: True, _FULL_DIR_PREFIX: True},
        )


async def _ensure_full_chromium_installed() -> None:
    """Install full chromium on demand, e.g. before the headed login launch.

    A no-op once full chromium is present. Used by the lazy path so the headed
    interactive-login fallback never launches against a shell-only install.
    """
    if full_chromium_ready():
        return
    browser_dir = configure_browser_environment()
    secure_mkdir(browser_dir)
    if not shell_ready():
        await _run_patchright_install("--only-shell")
        # Record the shell before the full stage so a --no-shell failure leaves
        # the shell marked ready and a retry skips re-installing it.
        _write_install_metadata(
            browser_dir,
            {_SHELL_DIR_PREFIX: True, _FULL_DIR_PREFIX: False},
        )
    await _run_patchright_install("--no-shell")
    _write_install_metadata(
        browser_dir,
        {_SHELL_DIR_PREFIX: True, _FULL_DIR_PREFIX: True},
    )


def ensure_browser_installed(*, full: bool = False) -> None:
    """Install the configured browser runtime a CLI mode needs, if absent.

    Used by CLI modes (--login, --status, --import-from-browser) to guarantee
    the right binary exists before launching it. Camoufox uses the same fetched
    Firefox artifact in both modes. Patchright's ``--status`` and import paths
    need only the shell, while ``--login`` needs full Chromium. The normal
    server path uses async background setup instead (non-blocking).
    """
    configure_browser_environment()
    if _engine_self_manages_binary():
        if camoufox_ready():
            return
        print("   Installing Camoufox browser...")
        try:
            asyncio.run(_run_camoufox_fetch())
        except Exception as exc:
            print(f"   ❌ Browser installation failed: {exc}")
            raise
        print("   Browser installed.")
        return

    if full:
        if full_chromium_ready():
            return
    elif shell_ready():
        return
    print("   Installing Patchright Chromium browser...")
    try:
        if full:
            asyncio.run(_ensure_full_chromium_installed())
        else:
            asyncio.run(_run_install_shell_only())
    except Exception as exc:
        print(f"   ❌ Browser installation failed: {exc}")
        raise
    print("   Browser installed.")


async def _run_install_shell_only() -> None:
    """Install just the headless shell for the headless CLI modes."""
    browser_dir = configure_browser_environment()
    secure_mkdir(browser_dir)
    full_present = full_chromium_ready()
    await _run_patchright_install("--only-shell")
    _write_install_metadata(
        browser_dir,
        {_SHELL_DIR_PREFIX: True, _FULL_DIR_PREFIX: full_present},
    )


def _safe_task_done(task: asyncio.Task[None] | None) -> bool:
    return task is not None and task.done()


async def _refresh_background_task_state() -> None:
    if _safe_task_done(_state.setup_task):
        task = _state.setup_task
        assert task is not None
        _state.setup_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            _state.setup_state = SetupState.FAILED
            _state.last_error = "Browser setup task was cancelled"
            logger.warning("Browser setup task cancelled")
        except Exception as exc:
            _state.setup_state = SetupState.FAILED
            _state.last_error = str(exc)
            logger.warning("Browser setup failed: %s", exc)
        else:
            # Do not trust task completion alone: Camoufox CLI releases have
            # returned success after printing a fetch failure, and a binary can
            # disappear between installation and reconciliation.
            if _browser_setup_ready():
                _state.setup_state = SetupState.READY
                _state.setup_completed_at = utcnow_iso()
            else:
                _state.setup_state = SetupState.FAILED
                _state.last_error = (
                    "Browser setup completed but the configured binary is unavailable"
                )
                logger.warning(_state.last_error)

    if _safe_task_done(_state.login_task):
        task = _state.login_task
        assert task is not None
        _state.login_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            _state.auth_state = AuthState.FAILED
            _state.last_error = "LinkedIn login bootstrap task was cancelled"
            logger.warning("LinkedIn login bootstrap task cancelled")
        except Exception as exc:
            _state.auth_state = AuthState.FAILED
            _state.last_error = str(exc)
            logger.warning("LinkedIn login bootstrap failed: %s", exc)
        else:
            _state.auth_state = AuthState.READY
            _state.auth_completed_at = utcnow_iso()


async def ensure_tool_ready_or_raise(
    tool_name: str, ctx: Context | None = None
) -> None:
    """Gate scrape/search tools on browser setup and authentication readiness."""
    initialize_bootstrap()
    await _refresh_background_task_state()

    if get_runtime_policy() == RuntimePolicy.DOCKER:
        if _engine_self_manages_binary() and not camoufox_ready():
            _state.setup_state = SetupState.FAILED
            _state.last_error = (
                "Camoufox browser binary is missing from the Docker image"
            )
            raise BrowserSetupFailedError(
                f"{_state.last_error}. Rebuild or update the image; the binary "
                "and its runtime assets must be baked for pwuser's HOME during "
                "the image build."
            )
        _raise_if_docker_auth_missing()
        return

    if _uses_custom_chrome():
        # The operator-provisioned Patchright executable bypasses background
        # setup; jump straight to the auth gate.
        _state.setup_state = SetupState.READY
        _state.setup_completed_at = _state.setup_completed_at or utcnow_iso()
        if _auth_ready():
            _state.auth_state = AuthState.READY
            return
        await _start_login_if_needed(ctx)
        return

    if _browser_setup_ready():
        _state.setup_state = SetupState.READY
    else:
        if _state.setup_state == SetupState.READY:
            invalidate_browser_setup()
        if _state.setup_state in {SetupState.IDLE, SetupState.FAILED} and (
            _state.setup_task is None or _state.setup_task.done()
        ):
            await start_background_browser_setup_if_needed()
        browser_name = (
            "Camoufox" if _engine_self_manages_binary() else "Patchright Chromium"
        )
        if ctx is not None:
            await ctx.report_progress(
                progress=5,
                total=100,
                message=f"{tool_name}: {browser_name} browser setup still in progress",
            )
        raise BrowserSetupInProgressError(
            "LinkedIn setup is not complete yet: the server is downloading the "
            f"{browser_name} browser in the background and will use it "
            "automatically once ready. Do not start a second manual browser "
            "installation or restart the server while this is running. Just wait "
            "and call this tool again in a minute or two."
        )

    if _auth_ready():
        _state.auth_state = AuthState.READY
        return

    await _start_login_if_needed(ctx)


def _raise_if_docker_auth_missing() -> None:
    if _auth_ready():
        return
    raise DockerHostLoginRequiredError(
        "No valid LinkedIn session is available in Docker. Run --login on the host machine to create a session, then retry this tool."
    )


def _auth_ready() -> bool:
    profile_dir = get_profile_dir()
    return (
        portable_cookie_is_valid(profile_dir)
        and source_state_path(profile_dir).exists()
        and _has_source_state()
    )


def _has_source_state() -> bool:
    try:
        get_authentication_source()
    except Exception:
        return False
    return True


def _auto_import_allowed() -> bool:
    """Return whether a silent browser-session import is safe to attempt now.

    Auto-import is ON BY DEFAULT. Locale-independent: keys off the config flag,
    the runtime policy, and the transport bind address -- never any displayed
    UI string. The flag check MUST stay first (test fakes and the tri-state
    'auto' resolution depend on it): None (default) and True both enable it,
    only an explicit False disables it.

    The two hard limits stay: Docker (no host browser/keychain) and a
    non-loopback streamable-http bind (a network-exposed HTTP daemon must not
    harvest a host cookie on a remote request). Note this covers network-exposed
    HTTP only, NOT stdio-over-SSH: a non-console session simply fails to decrypt
    the local user's keychain and degrades to manual login, and no cookie
    crosses the network.
    """
    config = get_config()
    if config.browser.auto_import_from_browser is False:
        return False
    if get_runtime_policy() == RuntimePolicy.DOCKER:
        # No host browser and no keychain inside a container.
        return False
    # A network-exposed HTTP daemon must never silently harvest a cookie on a
    # request from a remote client. Gate on the BIND ADDRESS, not the transport
    # type: a streamable-http server on a loopback host is the documented local
    # dev / verify flow and IS a desktop case; only a non-loopback bind is the
    # service case. This is an exact-match loopback allowlist that fails closed:
    # any unrecognized host (0.0.0.0, ::, a LAN IP, an IPv4-mapped loopback)
    # is treated as non-loopback and gated OFF.
    if config.server.transport == "streamable-http" and config.server.host not in (
        "127.0.0.1",
        "::1",
        "localhost",
    ):
        return False
    return True


def _pending_login_message(prior_error: str | None) -> str:
    """Poll-friendly wording for a still-pending login (not a failure)."""
    base = (
        "A LinkedIn login window is open and login is still in progress. "
        "This is not a failure. Complete the sign-in in the browser, then "
        "call this exact tool again in about 30 seconds to resume."
    )
    if prior_error:
        return f"{base} The previous login attempt did not finish: {prior_error}"
    return base


_AUTO_IMPORT_ANNOUNCED = False


async def _announce_auto_import_once(ctx: Context | None) -> None:
    """Emit a single notice per process before the first auto-import.

    Routes through the MCP ``ctx`` when available so a Claude Desktop user (who
    never sees stdio server logs) is told why a keychain dialog may appear; also
    logs once for the server operator's record.
    """
    global _AUTO_IMPORT_ANNOUNCED
    if _AUTO_IMPORT_ANNOUNCED:
        return
    _AUTO_IMPORT_ANNOUNCED = True
    message = (
        "No LinkedIn session found; importing one from a locally logged-in "
        "browser. macOS may show a one-time keychain prompt. Set "
        "AUTO_IMPORT_FROM_BROWSER=false or pass --no-auto-import to disable."
    )
    logger.info(message)
    if ctx is not None:
        try:
            await ctx.info(message)
        except Exception:  # noqa: BLE001 - a notice failure must not block import
            logger.debug("ctx.info notice failed", exc_info=True)


async def _try_auto_import_session(ctx: Context | None = None) -> bool:
    """Attempt a one-shot browser-session import outside ``_lock``.

    Returns True only when a validated session was persisted (so ``_auth_ready()``
    is now True). Every expected "nothing to import" outcome -- no live session,
    app-bound-only cookies, keystore denial/timeout, or LinkedIn rejecting the
    cookies -- returns False so the caller falls through to manual login. Only an
    unexpected error propagates.

    NOTE: the import is a LAZY import (not a top-level one) on purpose -- the
    test suite patches
    ``linkedin_mcp_server.browser_import.orchestrate.import_session_from_browser``
    and relies on it being re-looked-up at call time. Do not hoist it.
    """
    from linkedin_mcp_server.browser_import.orchestrate import (
        import_session_from_browser,
    )
    from linkedin_mcp_server.core.exceptions import AuthenticationError, NetworkError
    from linkedin_mcp_server.exceptions import (
        CookieDecryptionError,
        LinkedInMCPError,
        NoLinkedInSessionFoundError,
    )

    await _announce_auto_import_once(ctx)
    user_data_dir = get_profile_dir()
    # Import validation uses its own one-shot isolated profile. Close any
    # singleton first anyway so the bootstrap attempt has one browser owner and
    # a teardown failure cannot be misreported as an authentication verdict.
    if not await close_browser():
        raise NetworkError("Could not confirm browser teardown before session import")
    prev_headless = current_headless()
    set_headless(True)  # background probe; never pop a visible window
    try:
        # Hard ceiling on the complete lock + discovery + decryption + isolated
        # live-validation transaction. BrowserManager has its own launch bound;
        # this outer budget also covers waiting for the source lock and page
        # navigation. Keychain reads are independently bounded.
        async with asyncio.timeout(60):
            async with source_session_lock(user_data_dir):
                # Another server may have repaired the source session while this
                # process waited for the cross-process lock. Never overwrite it
                # with another staged import in that case.
                if _auth_ready():
                    return True
                result = await import_session_from_browser(
                    None, user_data_dir=user_data_dir
                )
        if not result:
            # Reached only when a live li_at decrypted but LinkedIn rejected the
            # session (orchestrate.py:254). The "no live session" and "could not
            # decrypt" cases RAISE and are handled below.
            logger.info(
                "Auto-import found no usable browser session; "
                "falling back to manual login"
            )
        return result
    except TimeoutError:
        logger.info("Auto-import timed out after 60s; falling back to manual login")
        return False
    except (
        NoLinkedInSessionFoundError,
        CookieDecryptionError,
        AuthenticationError,
        NetworkError,
        LinkedInMCPError,
    ) as exc:
        logger.info("Auto-import unavailable; falling back to manual login: %s", exc)
        return False
    finally:
        set_headless(prev_headless)


async def _start_login_if_needed(ctx: Context | None = None) -> None:
    # Cheap check-and-claim under the lock; the slow work (auto-import browser
    # launch, then the bounded inline wait) runs AFTER the lock is released so
    # concurrent pollers never serialize on it.
    async with _lock:
        await _refresh_background_task_state()

        if _auth_ready():
            _state.auth_state = AuthState.READY
            return

        login_task: asyncio.Task[None] | None = None
        import_task: asyncio.Task[bool] | None = None
        prior_error: str | None = None

        if _state.login_task is not None and not _state.login_task.done():
            # A manual login is already running: await the SAME task. Never
            # start an import on top of an in-flight headed login.
            login_task = _state.login_task
        elif _state.import_task is not None and not _state.import_task.done():
            # Another poller's import is in flight: await IT, do NOT spawn a
            # headed login (both would open a persistent context on the same
            # user_data_dir and collide on Chromium's SingletonLock).
            import_task = _state.import_task
        elif not _state.import_attempted and _auto_import_allowed():
            # Claim the one-shot import under the lock so only one keychain read
            # / import browser ever runs per process episode.
            _state.import_attempted = True
            _state.import_task = asyncio.create_task(
                _try_auto_import_session(ctx), name="linkedin-auto-import"
            )
            import_task = _state.import_task
        else:
            prior_error = _state.last_error

    # ---- lock released ----

    # Await an import (ours or a peer's). On success the caller falls through to
    # the scrape; on failure we re-enter to take the manual-login path.
    if import_task is not None:
        try:
            await import_task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - any import failure -> manual login
            logger.debug("Auto-import task failed", exc_info=True)
        async with _lock:
            await _refresh_background_task_state()
            if _auth_ready():
                _state.auth_state = AuthState.READY
                return
        # Import resolved without a session -> manual-login path. Re-enter:
        # import_attempted is now True and import_task is done, so this call
        # takes the spawn/await-login branch (no recursion loop risk).
        return await _start_login_if_needed(ctx)

    # No import in flight and none claimed -> the #535 manual-login + inline-wait
    # fallback. Spawn the login task if one is not already shared.
    if login_task is None:
        async with _lock:
            await _refresh_background_task_state()
            if _auth_ready():
                _state.auth_state = AuthState.READY
                return
            if _state.login_task is not None and not _state.login_task.done():
                login_task = _state.login_task
                prior_error = None
            else:
                prior_error = _state.last_error
                _state.auth_state = AuthState.STARTING
                _state.auth_started_at = utcnow_iso()
                _state.last_error = None
                _state.auth_completed_at = None
                _state.login_task = asyncio.create_task(
                    _run_login_flow(), name="linkedin-login"
                )
                login_task = _state.login_task

    # ---- #535 inline wait: unchanged logic ----
    budget = get_config().browser.login_inline_wait_seconds
    if budget and budget > 0:
        # asyncio.wait (NOT wait_for) leaves the task RUNNING on timeout; a
        # budget-elapsed wait must never cancel the in-progress login browser.
        await asyncio.wait({login_task}, timeout=budget)
        # Reconcile a finished task (nulls login_task, sets auth_state) before
        # reading readiness; success is filesystem truth via _auth_ready().
        await _refresh_background_task_state()
        if _auth_ready():
            _state.auth_state = AuthState.READY
            # Resume one-shot: the caller falls through to
            # get_or_create_browser()/ensure_authenticated()/scrape.
            return

    # Budget elapsed (still running), budget == 0, or the task finished but did
    # not persist a valid session. Emit the poll-friendly pending signal.
    if ctx is not None:
        await ctx.report_progress(
            progress=25,
            total=100,
            message="LinkedIn login in progress",
        )
    raise AuthenticationInProgressError(_pending_login_message(prior_error))


async def start_login_if_needed(ctx: Context | None = None) -> None:
    """Public wrapper for starting the shared login workflow."""
    await _start_login_if_needed(ctx)


async def invalidate_auth_and_trigger_relogin(
    ctx: Context | None = None,
) -> NoReturn:
    """Force-invalidate stale auth state and trigger interactive login.

    Unlike ``_start_login_if_needed()``, this ignores ``_auth_ready()`` — the
    caller has already proven the session is invalid despite profile files
    being present on disk.  The check-task → force-move → start-login sequence
    is atomic under ``_lock`` so an in-flight login is never corrupted.

    Raises:
        AuthenticationStartedError: Login browser opened.
        AuthenticationInProgressError: Login already running from a prior call.
    """
    logger.warning("Invalidating stale auth state and triggering re-login")
    async with _lock:
        await _refresh_background_task_state()

        # If a login is already in progress, don't touch files — just report.
        if _state.login_task is not None and not _state.login_task.done():
            if ctx is not None:
                await ctx.report_progress(
                    progress=25,
                    total=100,
                    message="LinkedIn login already in progress",
                )
            raise AuthenticationInProgressError(
                "No valid LinkedIn session is available yet. LinkedIn login is "
                "already in progress in a browser window. Complete login there, "
                "then retry this tool."
            )

        # Defer filesystem mutation to the login task. It acquires the
        # cross-process source-session lock first, so it cannot move a profile
        # another server is currently importing into or using for login.
        _state.force_auth_reset = True
        source_state = load_source_state(get_profile_dir())
        _state.force_auth_reset_generation = (
            source_state.login_generation if source_state is not None else None
        )

        # A force-move starts a fresh no-session episode; allow auto-import to
        # be re-attempted on the next tool call (the prior latch was for the
        # previous episode only). Auto-import fires at most once per episode.
        _state.import_attempted = False
        _state.import_task = None

        # Start fresh login.
        _state.auth_state = AuthState.STARTING
        _state.auth_started_at = utcnow_iso()
        _state.last_error = None
        _state.auth_completed_at = None
        _state.login_task = asyncio.create_task(
            _run_login_flow(), name="linkedin-login"
        )

    if ctx is not None:
        await ctx.report_progress(
            progress=25,
            total=100,
            message="LinkedIn login browser opened",
        )
    raise AuthenticationStartedError(
        "Session expired. A login browser window has been opened. "
        "Sign in with your LinkedIn credentials there, then retry this tool."
    )


def _move_auth_state_aside(*, force: bool = False) -> None:
    """Move auth artifacts to a timestamped backup directory.

    Args:
        force: If True, skip the ``_auth_ready()`` guard.  Used by
            ``invalidate_auth_and_trigger_relogin`` when the caller already
            knows the session is stale.
    """
    profile_dir = get_profile_dir()
    if not force and _auth_ready():
        return
    move_artifacts_aside(
        [
            profile_dir,
            portable_cookie_path(profile_dir),
            source_state_path(profile_dir),
            camoufox_identity_path(profile_dir),
        ],
        profile_dir,
    )


def _force_move_auth_state_aside() -> None:
    """Move auth artifacts aside unconditionally (no ``_auth_ready()`` guard)."""
    _move_auth_state_aside(force=True)


def _move_invalid_auth_state_aside() -> None:
    _move_auth_state_aside(force=False)


async def _run_login_flow() -> None:
    _state.auth_state = AuthState.IN_PROGRESS
    # The manual-login fallback launches headed, which needs full chromium.
    # In the default headless flow only the shell is installed eagerly, so
    # install full chromium here before the headed launch. A no-op once present
    # and skipped entirely for a custom executable or Camoufox. The
    # dependencies.py binary-missing backstop remains as a recovery path.
    if not _skips_managed_binary():
        await _ensure_full_chromium_installed()
    profile_dir = get_profile_dir()
    async with source_session_lock(profile_dir):
        force_reset = _state.force_auth_reset
        requested_generation = _state.force_auth_reset_generation
        if force_reset and _auth_ready():
            current_source_state = load_source_state(profile_dir)
            current_generation = (
                current_source_state.login_generation
                if current_source_state is not None
                else None
            )
            if current_generation != requested_generation:
                # A different process refreshed the source while this login
                # task waited. Its new generation supersedes our stale verdict.
                _state.force_auth_reset = False
                _state.force_auth_reset_generation = None
                return
        if not force_reset and _auth_ready():
            return
        if force_reset:
            _force_move_auth_state_aside()
        else:
            _move_invalid_auth_state_aside()
        _state.force_auth_reset = False
        _state.force_auth_reset_generation = None
        success = await interactive_login(profile_dir)
    if not success:
        raise AuthenticationBootstrapFailedError(
            "LinkedIn login was not completed. Retry the tool call to reopen the browser and continue setup."
        )
