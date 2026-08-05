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
import sys
from typing import NoReturn

from fastmcp import Context

from linkedin_mcp_server.common_utils import secure_mkdir, secure_write_text, utcnow_iso
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.config.schema import is_loopback_host
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
    AuthMissingOnOwnerError,
    AuthStaleOnOwnerError,
    BrowserSetupFailedError,
    BrowserSetupInProgressError,
    DockerHostLoginRequiredError,
)
from linkedin_mcp_server.server_role import ServerRole, process_role
from linkedin_mcp_server.session_state import (
    PeerSessionInPlaceError,
    auth_root_dir,
    get_runtime_id,
    load_source_state,
    portable_cookie_path,
    profile_exists,
    rotate_source_profile,
    source_state_path,
)
from linkedin_mcp_server.setup import UNGUARDED, interactive_login

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

# On-disk dir prefix of the headless shell. Nothing launches it any more —
# every launch names ``channel="chromium"`` — but the prefix is still needed to
# recognise one in an install written before that change.
_SHELL_DIR_PREFIX = "chromium_headless_shell-"
# On-disk dir prefix of full Chrome for Testing: the browser this server runs,
# in either mode.
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
    #: The login generation the in-flight login was told is broken, so it can
    #: stand down if a peer signs in first. On the state rather than the task's
    #: signature: `_run_login_flow` is spawned as a bare task in several places
    #: and stubbed in as many tests, and threading an argument through all of
    #: them would change far more than the one thing that matters.
    login_supersedes: str | None | object = UNGUARDED


_state = BootstrapState()
_lock = asyncio.Lock()

#: Set while this process is the shared owner and has found auth it cannot fix.
#: Deliberately not on BootstrapState: that is reset wholesale between tests, and
#: quiescence has its own reset so the two cannot be confused for one another.
_auth_quiescent = False
#: The generation that was broken. ``None`` is a value (a rotated profile reads as
#: exactly that), so it cannot double as "not set"; ``_auth_quiescent`` does that.
_auth_quiescent_generation: str | None = None


def reset_bootstrap_for_testing() -> None:
    """Reset bootstrap singleton state for test isolation."""
    global _state, _lock, _AUTO_IMPORT_ANNOUNCED
    global _auth_quiescent, _auth_quiescent_generation
    for task in (_state.setup_task, _state.login_task, _state.import_task):
        if task is not None and not task.done():
            task.cancel()
    _state = BootstrapState()
    _lock = asyncio.Lock()
    _AUTO_IMPORT_ANNOUNCED = False
    _state.login_supersedes = UNGUARDED
    _auth_quiescent = False
    _auth_quiescent_generation = None
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


def browser_ready() -> bool:
    """Return whether the full Chrome for Testing is installed and current.

    Only the full browser, and deliberately not the headless shell. Every launch
    now names ``channel="chromium"``, so the shell is never started and its
    absence is not a reason to reinstall anything.

    The previous version of this checked *every* install-by-default target, so a
    full-only install read as permanently not ready. Combined with a launch that
    demands the full browser, that is an unbounded loop: the gate opens on a
    shell-only install, the launch fails, only the metadata is invalidated, and
    the next setup installs the shell again.

    Pure: no mutation.
    """
    configured = _metadata_shape_ok()
    if configured is None:
        return False
    targets = _patchright_install_targets()
    if not targets:
        return False
    revision = targets.get(_FULL_DIR_PREFIX)
    if revision is None:
        return False
    return _has_install_for(configured, _FULL_DIR_PREFIX, revision)


def browser_setup_ready() -> bool:
    """Return whether the browser this server launches is installed and current.

    No longer mode-aware. ``headless`` selects a *mode*, not a binary, so it has
    nothing to say about which install is required. Pure: no mutation of
    metadata or in-memory state. Mutation happens in
    :func:`invalidate_browser_setup`, called by the gate paths.
    """
    return browser_ready()


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

    The patchright registry lock serializes concurrent installs, so two
    processes reaching this at once queue on the same browsers path rather than
    corrupting it.
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


async def _run_browser_setup() -> None:
    """Install full Chrome for Testing, in one stage.

    The two-stage shell-first arrangement existed to make the headless path
    usable sooner. Nothing launches the shell now, so staging it would download
    92 MiB nobody runs.

    Worth being straight about the effect on download size, because it cuts both
    ways. Measured against the CDN for 148.0.7778.96 mac-arm64: this is 170 MiB,
    against 263 MiB for anyone who previously ended up needing the full browser,
    and against 92 MiB for the default headless user who only ever fetched the
    shell. The second comparison is the one users will notice.
    """
    browser_dir = configure_browser_environment()
    secure_mkdir(browser_dir)

    await _run_patchright_install("--no-shell")
    _write_install_metadata(
        browser_dir,
        {_SHELL_DIR_PREFIX: False, _FULL_DIR_PREFIX: True},
    )


async def _ensure_browser_installed() -> None:
    """Install the browser on demand. A no-op once it is present."""
    if browser_ready():
        return
    await _run_browser_setup()


def ensure_browser_installed() -> None:
    """Install the Patchright Chromium browser for a CLI mode, if absent.

    Used by ``--login``, ``--status`` and ``--import-from-browser``. They no
    longer differ in what they need: the mode each runs in selects headed or
    headless behaviour, and both come from the same binary. The normal server
    path uses async background setup instead (non-blocking).
    """
    configure_browser_environment()
    # An operator-supplied executable is the one that gets launched, so the
    # managed browser would be downloaded and never run. That was survivable
    # while two of these three modes needed only the 92 MiB shell; now they all
    # want the full browser, so it is 170 MiB spent on nothing -- and for
    # someone whose network cannot reach the CDN, it is the difference between
    # signing in and not.
    if _uses_custom_chrome():
        return
    if browser_ready():
        return
    print("   Installing Patchright Chromium browser...")
    try:
        asyncio.run(_ensure_browser_installed())
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

    # Before any branch that could reach a browser. A quiescent owner has closed
    # Chromium and is waiting for a client to sign in; every path below would open
    # it again on the session that just failed, because readiness is decided by
    # whether the files exist rather than whether they work.
    _raise_if_auth_quiescent()

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
        await _start_login_if_needed(ctx, superseded_by=current_login_generation())
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

    # The generation goes with it, because this call has just looked: whatever is
    # on disk now is what it found wanting. A login started automatically by a
    # tool call is not somebody at a terminal insisting, so if a peer signs in
    # while this one is still getting there, standing down is right. `--login`
    # keeps its unguarded default, which is what makes it an override.
    await _start_login_if_needed(ctx, superseded_by=current_login_generation())


def _raise_if_docker_auth_missing() -> None:
    if _auth_ready():
        return
    raise DockerHostLoginRequiredError(
        "No valid LinkedIn session is available in Docker. Run --login on the host machine to create a session, then retry this tool."
    )


def _auth_ready(profile_dir: Path | None = None) -> bool:
    """Whether a session's files are all present under *profile_dir*.

    Defaults to the configured profile, which is what every gate here means. The
    argument exists for callers handed a directory explicitly, where asking about
    the configured one would answer a different question than the one asked, and
    silently: it would report the *default* profile ready while rotating another.
    """
    profile_dir = profile_dir or get_profile_dir()
    return (
        profile_exists(profile_dir)
        and portable_cookie_path(profile_dir).exists()
        and source_state_path(profile_dir).exists()
        # The file has to parse, not merely exist: a truncated write leaves one
        # behind that every existence check above is happy with.
        and load_source_state(profile_dir) is not None
    )


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
    if process_role() is ServerRole.OWNER:
        # The import runs headless, so the browser is not the problem: rotating
        # the profile is. It retires the current session before validating a
        # replacement, and the process that will actually log in needs to find
        # that session where the owner saw it. On macOS the keychain prompt
        # would also appear with nobody attached to approve it, so the read
        # simply times out.
        #
        # The frontend does it instead, and it is the better place for both
        # reasons: it has the desktop session, and it is the process that holds
        # the profile while it works.
        return False
    if config.browser.proxy_server:
        # The point of configuring a proxy is that LinkedIn sees one address.
        # A local browser's session was created on the real one, so silently
        # importing it and then driving it through the proxy produces exactly
        # the IP change that trips a security checkpoint. Explicit
        # --import-from-browser still works; only the automatic path defers to
        # --login through the proxy.
        logger.info(
            "Skipping auto-import: a proxy is configured, so a session from a "
            "local browser would move to a different address. Use --login to "
            "create the session through the proxy."
        )
        return False
    # A network-exposed HTTP daemon must never silently harvest a cookie on a
    # request from a remote client. Gate on the BIND ADDRESS, not the transport
    # type: a streamable-http server on a loopback host is the documented local
    # dev / verify flow and IS a desktop case; only a non-loopback bind is the
    # service case.
    if config.server.transport == "streamable-http" and not is_loopback_host(
        config.server.host
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
    from linkedin_mcp_server.core.exceptions import (
        AuthenticationError,
        NetworkError,
        ProxyConnectionError,
    )
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
            import_session_from_browser(
                None,
                user_data_dir=user_data_dir,
                # The import rotates the profile as soon as it holds the lease,
                # exactly as the login does, so it needs the same guard. Without
                # it a client whose keychain read ran long would retire a session
                # a faster peer had already imported.
                superseded_by=_state.login_supersedes,
            ),
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
    except ProxyConnectionError:
        # Ahead of NetworkError, which it subclasses. A dead proxy is not a
        # missing browser session: swallowing it here would hide the real cause
        # and fall back to a manual login that has to fail the same way.
        raise
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


async def _start_login_if_needed(
    ctx: Context | None = None, *, superseded_by: str | None | object = UNGUARDED
) -> None:
    if process_role() is ServerRole.OWNER:
        # Ahead of the lock, and ahead of every branch below, because all of them
        # end somewhere this process cannot go: an auto-import that rotates the
        # profile, or an interactive login window with nobody attached to answer
        # it.
        # Reporting from here leaves the session state exactly as the frontend
        # will find it, which is what lets the frontend take over cleanly.
        raise AuthMissingOnOwnerError(
            "The shared LinkedIn browser has no usable session, and it cannot "
            "sign in by itself. Retry this tool: the client will open a login "
            "window.",
            # The readiness gate runs before the tool body, so nothing has been
            # scraped and the client may run the call again once it has signed in.
            nothing_ran_yet=True,
        )

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
            _state.login_supersedes = superseded_by
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
        # Imported here, like the other core exceptions in this module, to keep
        # bootstrap out of the config -> core import cycle.
        from linkedin_mcp_server.core.exceptions import ProxyConnectionError

        try:
            await import_task
        except asyncio.CancelledError:
            raise
        except ProxyConnectionError:
            # The import itself re-raises this rather than reporting "no
            # session"; swallowing it here would undo that and send the user
            # into a manual login that has to fail through the same proxy.
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
        return await _start_login_if_needed(ctx, superseded_by=superseded_by)

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
                _move_invalid_auth_state_aside(superseded_by)
                _state.auth_state = AuthState.STARTING
                _state.auth_started_at = utcnow_iso()
                _state.last_error = None
                _state.auth_completed_at = None
                _state.login_supersedes = superseded_by
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


async def start_login_if_needed(
    ctx: Context | None = None, *, superseded_by: str | None | object = UNGUARDED
) -> None:
    """Public wrapper for starting the shared login workflow."""
    await _start_login_if_needed(ctx, superseded_by=superseded_by)


async def wait_for_login_to_finish(timeout: float) -> bool:
    """Wait for a login this process started, and say whether one now exists.

    The two functions that start a login both report "started" by raising, and
    they raise while the browser is still open, because the person at it has not
    typed anything yet. That is the right answer for a tool call, which cannot
    hold a client for half an hour. It is the wrong answer for the frontend
    repairing auth on the owner's behalf: there the raise arrives as a *failure*
    to sign in, and the call that could now be served is refused instead.

    So this is the one place that waits it out. It reads the task rather than
    polling readiness, because a login that fails must end the wait as surely as
    one that succeeds; and it returns filesystem truth rather than the task's
    result, because "a session exists" is the question the caller actually has.

    Returns False when the wait runs out, which leaves the login running: it owns
    the profile and cancelling it would strand a half-finished sign-in.
    """
    task = _state.login_task
    if task is not None and not task.done():
        # `wait`, never `wait_for`: the latter cancels on timeout, and this task
        # is a browser window somebody may be typing into.
        await asyncio.wait({task}, timeout=timeout)
    await _refresh_background_task_state()
    return _auth_ready()


def current_login_generation() -> str | None:
    """Which login generation is on disk now, or ``None`` when there is none.

    ``None`` is a value here rather than an absence: it is exactly what a rotated
    profile reads as, so the difference between "no session" and "a session I have
    not seen" has to be carried by something else.
    """
    state = load_source_state(get_profile_dir())
    return None if state is None else state.login_generation


def go_auth_quiescent(observed_generation: str | None) -> None:
    """Stop this owner touching the profile until a new session appears.

    Closing the browser is not enough on its own. ``get_or_create_browser`` opens
    one whenever ``_browser`` is None, and ``_auth_ready()`` tests whether the
    files exist rather than whether they work, so the next forwarded call would
    reopen Chromium on the same dead session, in the middle of the login the
    frontend is running. Measured before this existed: the call after a confirmed
    close created a second browser.

    *observed_generation* is the generation this owner found broken. The caller
    reads it before closing the browser, which is defensive rather than currently
    necessary: the tool middleware holds a lease reference across the whole call,
    so nothing else can write a generation in between. It costs nothing and stops
    a later caller, or a change that frees the lease sooner, from latching onto
    the generation written by the login it is about to ask for.
    """
    global _auth_quiescent_generation, _auth_quiescent
    _auth_quiescent = True
    _auth_quiescent_generation = observed_generation
    logger.warning(
        "The shared browser cannot sign in; waiting for a client to do it "
        "(generation %s)",
        observed_generation or "none",
    )


def _auth_quiescence_lifted() -> bool:
    """Whether a usable session has replaced the one that caused quiescence.

    Both halves are needed and each alone is wrong. A changed generation alone
    lifts on an *abandoned* login: the frontend rotates the profile first, which
    makes the generation read as ``None``, which differs from what was observed,
    and the owner would open Chromium on a profile with no session at all.
    ``_auth_ready()`` alone never lifts, because it tests file existence and was
    already true of the broken session.

    Together they mean what is wanted, because a generation is only written after
    a login has validated and exported its cookies.
    """
    return _auth_ready() and current_login_generation() != _auth_quiescent_generation


def _raise_if_auth_quiescent() -> None:
    """Report instead of opening a browser, until a new session lands."""
    global _auth_quiescent, _auth_quiescent_generation
    if not _auth_quiescent:
        return
    if _auth_quiescence_lifted():
        logger.info("A new LinkedIn session appeared; the shared browser resumes")
        _auth_quiescent = False
        _auth_quiescent_generation = None
        return
    raise AuthStaleOnOwnerError(
        "The shared LinkedIn browser's session stopped working, and it cannot "
        "sign in by itself. Retry this tool: the client will open a login "
        "window.",
        # Raised from the readiness gate, ahead of the tool body.
        nothing_ran_yet=True,
        # The generation this owner latched on, not a fresh reading. Every call
        # after the first arrives here rather than through `handle_auth_error`,
        # so leaving it out would mean the *second* client to ask gets a marker
        # with nothing to compare against, and repairs unguarded while the first
        # is still signing in.
        generation=_auth_quiescent_generation,
    )


async def invalidate_auth_and_trigger_relogin(
    ctx: Context | None = None,
    *,
    stale_generation: str | None | object = UNGUARDED,
) -> NoReturn:
    """Force-invalidate stale auth state and trigger interactive login.

    Unlike ``_start_login_if_needed()``, this ignores ``_auth_ready()`` — the
    caller has already proven the session is invalid despite profile files
    being present on disk.  The check-task → force-move → start-login sequence
    is atomic under ``_lock`` so an in-flight login is never corrupted.

    *stale_generation* is the login generation the caller found broken, which may
    be ``None`` when it found no session at all. Passing it is what keeps two
    clients from destroying each other's work: ``_lock`` is process-local, so it
    says nothing about the other process, and the rotation below is skipped when
    the generation on disk has moved on from the one being complained about.

    Left at :data:`UNGUARDED` only by a caller that has observed nothing, which
    is why the two are distinct values. Conflating them was measured to produce
    the worst available failure: a login that reports a window has opened, opens
    none, keeps the dead session and leaves readiness saying it is fine.

    Raises:
        AuthenticationStartedError: Login browser opened.
        AuthenticationInProgressError: Login already running from a prior call.
        PeerSessionInPlaceError: Someone else's fresh session is now on disk, so
            there is nothing to invalidate and no login to start. The caller may
            simply use it.
        AuthStaleOnOwnerError: This process is the shared owner, so it reports
            rather than rotating the profile and opening a login nobody could
            answer.
    """
    if process_role() is ServerRole.OWNER:
        # Never reaches the rotation below. Retiring the session here would race
        # the frontend's login for the same files, and the login window has
        # nowhere to appear. The caller has already closed the browser and
        # recorded the generation it found, which is what `go_auth_quiescent`
        # needs, so all that is left is to say so.
        # Reached only if something calls this directly; `handle_auth_error`
        # raises its own first, carrying whether any work had run. Conservative
        # here, because a direct caller has told us nothing about that.
        raise AuthStaleOnOwnerError(
            "The shared LinkedIn browser's session stopped working, and it "
            "cannot sign in by itself. Retry this tool: the client will open a "
            "login window."
        )

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
        #
        # A failure here used to stop the login, and that reasoning has expired.
        # It made sense when the login demanded the profile outright: if the
        # rotation could not take it, neither could the login, and saying so beat
        # claiming a browser had opened. Now the login *waits* for the profile,
        # so a momentary holder is precisely the case it handles, and refusing
        # here throws away the wait before it happens. Measured: with a lease held
        # for 1.5 seconds, this raised "the browser profile is in use" and the
        # 60-second wait was never reached.
        #
        # The rotation itself is not lost. `interactive_login` rotates under the
        # profile it waited for (`setup.py`, `rotate_shielded`), which is the
        # better place for it anyway: this one runs without holding anything, so
        # its result was never guaranteed to survive to the login. What is lost by
        # skipping it is only that `_auth_ready()` keeps reporting the dead
        # session until the login retires it. On a shared owner the quiescence
        # latch closes that window. On a single-process server nothing does, and
        # it does not need to: the same call is already committed to starting a
        # login, and the login retires the state itself once it holds the
        # profile. What must not happen is the login standing down there, which
        # is why the generation it was told about travels with it.
        try:
            _force_move_auth_state_aside(stale_generation)
        except AuthenticationBootstrapFailedError as exc:
            logger.info(
                "Could not retire the stale session yet (%s); the login will "
                "wait for the profile and retire it there",
                exc,
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
        _state.login_supersedes = stale_generation
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


def _move_auth_state_aside(
    *, force: bool = False, superseded_by: str | None | object = UNGUARDED
) -> None:
    """Move auth artifacts to a timestamped backup directory.

    Args:
        force: If True, skip the ``_auth_ready()`` guard.  Used by
            ``invalidate_auth_and_trigger_relogin`` when the caller already
            knows the session is stale.

    Raises:
        AuthenticationBootstrapFailedError: The state could not be retired.
            Whether that stops the caller depends on the caller. It used to have
            to: the login rotated the same artifacts and would have failed at the
            same point, after telling the user a browser had opened. The login
            waits for the profile now, so a caller that has one to start may
            reasonably carry on and let it retire the state under the lease it
            waited for.
    """
    if not force and _auth_ready():
        return
    # Quarantine creation lives in session_state so the routine rotation on a
    # new session and this stale-state path produce identically shaped backups.
    try:
        rotate_source_profile(get_profile_dir(), superseded_by=superseded_by)
    except PeerSessionInPlaceError:
        # Ahead of RuntimeError, which it subclasses, and deliberately not turned
        # into a bootstrap failure: nothing failed. Somebody else signed in, so
        # the caller must stop rather than go on to promise a login window that
        # will not open. Measured with it swallowed here: the caller was told
        # "a login browser window has been opened" and none was.
        raise
    except RuntimeError as exc:
        raise AuthenticationBootstrapFailedError(
            f"{exc} No login was started."
        ) from exc
    except OSError as exc:
        raise AuthenticationBootstrapFailedError(
            f"Could not retire the stale session: {exc}. No login was started."
        ) from exc


def _force_move_auth_state_aside(
    superseded_by: str | None | object = UNGUARDED,
) -> None:
    """Move auth artifacts aside unconditionally (no ``_auth_ready()`` guard)."""
    _move_auth_state_aside(force=True, superseded_by=superseded_by)


def _move_invalid_auth_state_aside(
    superseded_by: str | None | object = UNGUARDED,
) -> None:
    _move_auth_state_aside(force=False, superseded_by=superseded_by)


async def _run_login_flow() -> None:
    """Install what a headed login needs, then run one.

    The generation this login supersedes is read from the shared state rather
    than taken as an argument, and passed on rather than checked here: this runs
    as a background task, and the gap between this line and the profile actually
    being held is the whole window that matters.
    """
    _state.auth_state = AuthState.IN_PROGRESS
    # An idempotent backstop rather than a staging step: background setup now
    # installs the same browser this launch uses, so by the time anyone reaches
    # a login there is normally nothing to do. Kept because a login can be
    # reached before that setup finishes. Skipped for a custom executable, and
    # the dependencies.py binary-missing path remains the recovery route.
    if not _uses_custom_chrome():
        await _ensure_browser_installed()
    success = await interactive_login(
        get_profile_dir(), superseded_by=_state.login_supersedes
    )
    if not success:
        raise AuthenticationBootstrapFailedError(
            "LinkedIn login was not completed. Retry the tool call to reopen the browser and continue setup."
        )
