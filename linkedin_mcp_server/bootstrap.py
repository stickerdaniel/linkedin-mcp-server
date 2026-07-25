"""Managed runtime bootstrap for browser setup and LinkedIn login."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import functools
import importlib.metadata
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
_INSTALL_METADATA_SCHEMA = 3

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
    binary is never used and the background install is unnecessary.
    """
    return bool(get_config().browser.chrome_path)


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
        # A custom executable skips the managed binary; nothing to install.
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
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        output = "\n".join(
            text for text in (stderr.decode().strip(), stdout.decode().strip()) if text
        )
        raise BrowserSetupFailedError(
            output or "Patchright Chromium browser setup failed."
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
    """Install the headless shell first, then full chromium when needed.

    Stage one (``--only-shell``) lands the headless shell + ffmpeg so the
    headless first-run path becomes usable as early as possible; metadata is
    written after it so a crash before stage two still records the shell as
    ready. Stage two (``--no-shell``) adds full Chrome for Testing for the
    headed login fallback / ``--no-headless`` mode, and runs only when needed.
    """
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
    """Install the Patchright Chromium binaries a CLI mode needs, if absent.

    Used by CLI modes (--login, --status, --import-from-browser) to guarantee
    the right binary exists before launching it: ``--status`` and
    ``--import-from-browser`` run headless and need only the shell, while
    ``--login`` is headed and needs full chromium. The normal server path uses
    async background setup instead (non-blocking).
    """
    configure_browser_environment()
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

    if _uses_custom_chrome():
        # A custom executable bypasses the managed binary entirely, so the
        # background install is irrelevant; jump straight to the auth gate.
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
        if ctx is not None:
            await ctx.report_progress(
                progress=5,
                total=100,
                message=f"{tool_name}: Patchright Chromium browser setup still in progress",
            )
        raise BrowserSetupInProgressError(
            "LinkedIn setup is not complete yet: the server is downloading the "
            "Patchright Chromium browser in the background and will use it "
            "automatically once ready. Do not install the browser yourself (no "
            "`patchright install` or `uv run patchright install`), and do not "
            "restart the server. A manual install only fights the background one "
            "for the same lock and slows it down. Just wait and call this tool "
            "again in a minute or two."
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
    # The import opens a persistent context on user_data_dir; the singleton holds
    # a SingletonLock on that same dir, so release it first. No-op on the
    # no-session path (_browser is None); defensive on any relogin reuse.
    await close_browser()
    prev_headless = current_headless()
    set_headless(True)  # background probe; never pop a visible window
    try:
        # Hard ceiling on the whole import. The on-loop validation step launches
        # a persistent Chromium context (drivers/browser.py validate_imported_cookies
        # -> core/browser.py start) with NO launch timeout, so a wedged binary
        # (stale SingletonLock, sandbox stall, half-installed Chromium, X-less
        # Linux desktop) would otherwise hang the first no-session tool call.
        # Default-on routes every desktop first-call through this, so the bound
        # is what makes "fails fast and falls back" hold end to end. Keychain
        # reads are already bounded (security 10s / secret-tool 10s); this covers
        # the launch + navigation budget on top.
        result = await asyncio.wait_for(
            import_session_from_browser(None, user_data_dir=user_data_dir),
            timeout=60,
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
                _move_invalid_auth_state_aside()
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

        # Force-move stale profile files (skip _auth_ready() guard).
        _force_move_auth_state_aside()

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
    # The manual-login fallback launches headed, which needs full chromium.
    # In the default headless flow only the shell is installed eagerly, so
    # install full chromium here before the headed launch. A no-op once present
    # and skipped entirely for a custom executable. The dependencies.py
    # binary-missing backstop remains as a recovery path.
    if not _uses_custom_chrome():
        await _ensure_full_chromium_installed()
    success = await interactive_login(get_profile_dir())
    if not success:
        raise AuthenticationBootstrapFailedError(
            "LinkedIn login was not completed. Retry the tool call to reopen the browser and continue setup."
        )
