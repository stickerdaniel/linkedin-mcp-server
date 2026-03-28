"""Managed runtime bootstrap for browser setup and LinkedIn login."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.authentication import get_authentication_source
from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text, utcnow_iso
from linkedin_mcp_server.drivers.browser import get_profile_dir
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
    get_runtime_id,
    portable_cookie_path,
    profile_exists,
    runtime_profiles_root,
    source_state_path,
)
from linkedin_mcp_server.setup import interactive_login

logger = logging.getLogger(__name__)

_BROWSER_DIR = "patchright-browsers"
_BROWSER_INSTALL_METADATA = "browser-install.json"
_INVALID_STATE_PREFIX = "invalid-state-"


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
    initialized: bool = False


_state = BootstrapState()
_lock = asyncio.Lock()


def reset_bootstrap_for_testing() -> None:
    """Reset bootstrap singleton state for test isolation."""
    global _state, _lock
    for task in (_state.setup_task, _state.login_task):
        if task is not None and not task.done():
            task.cancel()
    _state = BootstrapState()
    _lock = asyncio.Lock()
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


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
    """Ensure the shared browser cache path is configured."""
    browser_dir = browsers_path()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))
    return browser_dir


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

    async with _lock:
        if _browser_setup_ready():
            _state.setup_state = SetupState.READY
            _state.setup_completed_at = _state.setup_completed_at or utcnow_iso()
            return
        if _state.setup_task is not None and not _state.setup_task.done():
            return
        _start_browser_setup_task_locked()


def browser_setup_ready() -> bool:
    metadata_path = install_metadata_path()
    configured_browsers_path = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(browsers_path()))
    )
    if not metadata_path.exists() or not configured_browsers_path.exists():
        return False
    if not any(configured_browsers_path.iterdir()):
        return False
    try:
        payload = json.loads(metadata_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("browser_name") == "chromium"
        and payload.get("installer_name") == "patchright"
    )


def _browser_setup_ready() -> bool:
    """Compatibility wrapper for tests and internal callers."""
    return browser_setup_ready()


def _start_browser_setup_task_locked() -> None:
    _state.setup_state = SetupState.RUNNING
    _state.setup_started_at = utcnow_iso()
    _state.last_error = None
    _state.setup_completed_at = None
    _state.setup_task = asyncio.create_task(_run_browser_setup(), name="browser-setup")


async def _run_browser_setup() -> None:
    browser_dir = configure_browser_environment()
    metadata_path = install_metadata_path()
    secure_mkdir(browser_dir)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "patchright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        output = "\n".join(
            text for text in (stderr.decode().strip(), stdout.decode().strip()) if text
        )
        raise BrowserSetupFailedError(
            output or "Patchright Chromium browser setup failed."
        )

    metadata = {
        "version": 1,
        "runtime_id": get_runtime_id(),
        "installed_at": utcnow_iso(),
        "browsers_path": str(browser_dir),
        "browser_name": "chromium",
        "installer_name": "patchright",
    }
    secure_write_text(
        metadata_path, json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    )


def ensure_browser_installed() -> None:
    """Install Patchright Chromium synchronously if not already present.

    Used by CLI modes (--login, --status) to guarantee the browser exists
    before launching it.  The normal server path uses async background setup
    instead (non-blocking).
    """
    configure_browser_environment()
    if browser_setup_ready():
        return
    print("   Installing Patchright Chromium browser...")
    try:
        asyncio.run(_run_browser_setup())
    except Exception as exc:
        print(f"   ❌ Browser installation failed: {exc}")
        raise
    print("   Browser installed.")


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
            logger.warning("Patchright Chromium browser setup task cancelled")
        except Exception as exc:
            _state.setup_state = SetupState.FAILED
            _state.last_error = str(exc)
            logger.warning("Patchright Chromium browser setup failed: %s", exc)
        else:
            _state.setup_state = SetupState.READY
            _state.setup_completed_at = utcnow_iso()

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
        _raise_if_docker_auth_missing()
        return

    if _browser_setup_ready():
        _state.setup_state = SetupState.READY
    else:
        if _state.setup_state in {SetupState.IDLE, SetupState.FAILED} and (
            _state.setup_task is None or _state.setup_task.done()
        ):
            await start_background_browser_setup_if_needed()
        if ctx is not None:
            await ctx.report_progress(
                progress=5,
                total=100,
                message=f"{tool_name}: Patchright Chromium browser setup still in progress",
            )
        raise BrowserSetupInProgressError(
            "LinkedIn setup is not complete yet. The Patchright Chromium browser is still downloading in the background. Retry this tool in a few minutes."
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
        profile_exists(profile_dir)
        and portable_cookie_path(profile_dir).exists()
        and source_state_path(profile_dir).exists()
        and _has_source_state()
    )


def _has_source_state() -> bool:
    try:
        get_authentication_source()
    except Exception:
        return False
    return True


async def _start_login_if_needed(ctx: Context | None = None) -> None:
    async with _lock:
        await _refresh_background_task_state()

        if _auth_ready():
            _state.auth_state = AuthState.READY
            return

        if _state.login_task is not None and not _state.login_task.done():
            if ctx is not None:
                await ctx.report_progress(
                    progress=25,
                    total=100,
                    message="LinkedIn login already in progress",
                )
            raise AuthenticationInProgressError(
                "No valid LinkedIn session is available yet. LinkedIn login is already in progress in a browser window. Complete login there, then retry this tool."
            )

        _move_invalid_auth_state_aside()
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
        "No valid LinkedIn session was found. A login browser window has been opened. Sign in with your LinkedIn credentials there, then retry this tool."
    )


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

        # Force-move stale profile files (skip _auth_ready() guard).
        _force_move_auth_state_aside()

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
    targets = [
        profile_dir,
        portable_cookie_path(profile_dir),
        source_state_path(profile_dir),
        runtime_profiles_root(profile_dir),
    ]
    existing = [target for target in targets if target.exists()]
    if not existing:
        return
    if not force and _auth_ready():
        return

    backup_dir = (
        auth_root_dir(profile_dir)
        / f"{_INVALID_STATE_PREFIX}{utcnow_iso().replace(':', '-')}"
    )
    secure_mkdir(backup_dir)
    for target in existing:
        shutil.move(str(target), str(backup_dir / target.name))


def _force_move_auth_state_aside() -> None:
    """Move auth artifacts aside unconditionally (no ``_auth_ready()`` guard)."""
    _move_auth_state_aside(force=True)


def _move_invalid_auth_state_aside() -> None:
    _move_auth_state_aside(force=False)


async def _run_login_flow() -> None:
    _state.auth_state = AuthState.IN_PROGRESS
    success = await interactive_login(get_profile_dir(), warm_up=True)
    if not success:
        raise AuthenticationBootstrapFailedError(
            "LinkedIn login was not completed. Retry the tool call to reopen the browser and continue setup."
        )
