"""Managed runtime bootstrap for browser setup and LinkedIn login."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
import contextlib
from dataclasses import dataclass
from enum import Enum
import functools
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys
import time
from typing import Any, NoReturn

from fastmcp import Context
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.spinner import Spinner
from rich.theme import Theme

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
from linkedin_mcp_server.profile_lease import (
    ProfileLeaseUnavailableError,
    _release_locked_fd,
    acquire_locked_fd,
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

# Sidecar recording the cache state the retained-revision warning last named. It
# lives inside the configured browsers path so it travels with the cache it
# describes, and its name starts with a dot so patchright's own collector passes
# over it: that collector deletes directories whose name begins with a browser
# name, and link files under `.links`, and nothing else.
_CACHE_REPORT_MARKER = ".linkedin-mcp-cache-report.json"
_CACHE_REPORT_LOCK = ".linkedin-mcp-cache-report.lock"
_CACHE_REPORT_SCHEMA = 1

#: How much of the installer's output a failure message may quote. Patchright
#: embeds a whole non-200 response body in one error, and retries five times, so
#: an authenticated mirror answering with a page rather than a browser can emit
#: arbitrarily much. The tail is what says why it failed. Bounded by characters
#: as well as by count, because a fragment can itself be 64 KiB: two hundred of
#: those would put 12 MiB into an exception message and into ``last_error``.
_MAX_RETAINED_LINES = 200
_MAX_RETAINED_CHARS = 64 * 1024
#: Read size for the installer's pipe.
_READ_CHUNK = 64 * 1024
#: A run of output this long with no newline in it is emitted as one line rather
#: than buffered further. Bounds memory for output that never terminates a line.
_MAX_LINE_CHARS = 64 * 1024
#: Any userinfo in a URL, not only the ``user:password`` form. Patchright prints
#: the download URL it resolved, and ``PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`` may
#: carry credentials for an internal mirror. A bare ``//TOKEN@host`` is as much
#: a secret as a pair, and so are ``//TOKEN:@host`` and ``//:TOKEN@host``, which
#: a pattern requiring both halves lets through. Greedy to the *last* ``@``: a
#: password may contain one, and stopping at the first leaves the rest of it in
#: the line. Backslashes too, which Node normalises to slashes before
#: authenticating, so ``https:\\user:pw@host`` is a working credential.
_CREDENTIALS_IN_URL = re.compile(r"[/\\]{2}[^/\\\s]*@")
#: The query of any URL. A mirror can carry its credential there as easily as
#: in the userinfo, patchright pastes its download path onto whatever
#: ``PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST`` names, and no list of parameter names
#: is ever complete: ``?token=``, ``?auth_token=``, ``?x-api-key=`` and
#: ``?X-Amz-Signature=`` are all in use. So the whole query goes, and the part
#: that identifies the mirror stays. Stopping at an apostrophe as well as at
#: whitespace, because the run would otherwise reach past the quote that closes
#: a response body and swallow the ``'. URL: `` that ends it, which is what
#: ``_installer_lines`` looks for. A query holding a literal apostrophe is not
#: something patchright constructs.
_QUERY_IN_URL = re.compile(r"(?i)(\bhttps?:[/\\]{2}[^\s?']*)\?[^\s']*")
#: C0 and C1 controls and escape sequences. The eight-bit C1 forms do the same
#: work as their ESC pairs on terminals that accept them, so U+009B followed by
#: "2J" clears a screen exactly as "\x1b[2J" does. Patchright quotes a whole non-200 response
#: body, and a body can carry the sequence that clears a terminal or renames
#: its window. rich strips these from what it renders; the debug log writes
#: straight to a stream and would not.
_TERMINAL_CONTROLS = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[a-zA-Z]|[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]"
)
#: ``Download failed: server returned code 403 body '…'. URL: …``, patchright's
#: one message that quotes what a server sent it (``coreBundle.js``, in
#: ``downloadFile``; every other download error carries a status code, a size
#: or a URL). Whatever is between those two markers is a stranger's bytes on a
#: developer's terminal, so it is dropped rather than sanitised: it is where the
#: escape sequences, the reflected credentials, the markup and the absurd
#: numbers all came from, and none of it says anything the status code does not.
#: The body is not one line: ``content += chunk`` collects an HTML error page
#: with its newlines intact, so the closing marker can arrive thousands of lines
#: later.
_RESPONSE_BODY_OPENS = re.compile(r"server returned code \d{1,3} body '")
_RESPONSE_BODY_CLOSES = "'. URL: "
#: The closing marker as patchright writes it: the marker, the download URL,
#: end of line. Anchored, because the body between the markers is a stranger's
#: bytes and can hold the marker itself: measured against a refusing mirror,
#: the real closer starts a line of its own and its URL runs to the end of it,
#: so a marker with prose behind it is the body talking and the drop stays
#: open. This narrows the forgery to a body line that ends in a URL-shaped
#: token; it cannot close it, because the grammar is ambiguous at the source.
#: What the drop is *for* does not rest on that: the escape sequences are
#: stripped, the credentials redacted, the markup disabled and the line length
#: capped whether or not a body is recognised as one.
_RESPONSE_BODY_CLOSED = re.compile(r"'\. URL: \S*\Z")
#: One short of the marker, which is the most of it that a forced cut can leave
#: on the far side of a fragment boundary.
_CLOSER_CARRY = len(_RESPONSE_BODY_CLOSES) - 1
_OMITTED_BODY = "<response body omitted>"

#: ``|■■■■    |  30% of 187.2 MiB``: the progress line patchright writes when
#: its stdout is a pipe, which is always the case here. The digit counts are the
#: guard: patchright's error output carries a whole response body, and a
#: progress-shaped line from one raised ``OverflowError`` on a 400-digit size
#: and hit Python's 4300-digit integer limit on a long percentage. Bounded here,
#: neither can be constructed.
_PATCHRIGHT_PERCENT = re.compile(r"\|\s*(\d{1,3})%\s+of\s+([\d.]{1,15})\s*([KMG]i?B)")
#: ``Downloading Chrome for Testing 149.0.7827.55 (…) from https://…``
_PATCHRIGHT_DOWNLOAD = re.compile(r"^Downloading (.+?) from ")
#: ``Chrome for Testing 149.0.7827.55 (…) downloaded to /…/chromium-1228``. The
#: only completion signal there is when no percentage was ever reported.
_PATCHRIGHT_DONE = re.compile(r" downloaded to ")
_BINARY_UNITS = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}
#: LinkedIn's dark-mode blue, for the bar and the spinner alike. The brand blue
#: #0A66C2 is meant for light backgrounds and reads muted on a dark terminal,
#: which is where this runs. Only honoured where the terminal advertises
#: truecolor; rich approximates it to the nearest ANSI colour otherwise.
_PROGRESS_STYLE = "#70B5F9"
#: Everything non-ASCII this draws. The bar downgrades itself to "-" wherever
#: the console encoding is not utf (``ConsoleOptions.ascii_only``); the spinner
#: does not, and its braille goes into ``write`` as it is. Read off the spinner
#: rather than copied, so a rich release that redraws "dots" is measured here
#: instead of assumed.
_BAR_GLYPHS = "".join(Spinner("dots").frames)


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
    cache_report_task: asyncio.Task[None] | None = None
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
    for task in (
        _state.setup_task,
        _state.cache_report_task,
        _state.login_task,
        _state.import_task,
    ):
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


def _revision_dir_prefix(name: str) -> str | None:
    """Return the browser prefix of a revision directory, or ``None``.

    Only a numeric suffix counts, and only for the two browsers this server
    installs. Patchright can leave other directories in the same cache
    (``chromium_tip_of_tree-``, ``ffmpeg-``, a partially downloaded
    ``*.downloads-`` staging dir), and a report has nothing to say about a
    browser it never asked for.
    """
    for prefix in (_FULL_DIR_PREFIX, _SHELL_DIR_PREFIX):
        if name.startswith(prefix) and name[len(prefix) :].isdigit():
            return prefix
    return None


def _retained_revision_dirs(configured: Path, current_revision: str) -> list[Path]:
    """Return the browser directories in *configured* that will never launch again.

    The full Chromium at *current_revision* is the one every launch names, so it
    is active. Every older full revision is retained, and so is every headless
    shell at any revision: nothing launches the shell since the install became
    single-stage, and a cache written before that still holds one.

    Symlinks are skipped before anything else. A link is not the storage it
    points at, so counting it would attribute somebody else's bytes to this
    cache and name a directory the remedy below would not free.
    """
    retained: list[Path] = []
    for entry in sorted(configured.iterdir()):
        if entry.is_symlink() or not entry.is_dir():
            continue
        prefix = _revision_dir_prefix(entry.name)
        if prefix is None:
            continue
        if prefix == _FULL_DIR_PREFIX and entry.name[len(prefix) :] == current_revision:
            continue
        retained.append(entry)
    return retained


def _directory_size(path: Path) -> int:
    """Sum the bytes of the real files under *path*, following no symlink.

    A file that vanishes or turns unreadable mid-walk is skipped: an install
    running in another process may be writing into the same cache, and an
    approximate figure beats a diagnostic that raises.
    """
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
    return total


def _format_size(num_bytes: int) -> str:
    """Render a logical byte count in binary units."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def _cache_report_signature(
    current_revision: str, retained: list[Path]
) -> dict[str, object]:
    """Describe a cache state precisely enough to know when it has changed."""
    return {
        "version": _CACHE_REPORT_SCHEMA,
        "current_revision": current_revision,
        "retained": sorted(entry.name for entry in retained),
    }


def _cache_report_is_current(marker: Path, signature: dict[str, object]) -> bool:
    """Whether *marker* already records exactly this cache state.

    An unreadable or malformed marker answers False, so the report is made
    again. Warning twice costs a log line; staying silent about a gigabyte
    because a file could not be parsed costs the whole point of the report.
    """
    try:
        payload = json.loads(marker.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == signature


def _report_retained_browser_revisions() -> None:
    """Warn about retained revisions and suppress later reports of the same state.

    Patchright preserves every revision named by a valid reference under
    ``.links``, and uv keeps one readable archive per version anyone has ever
    resolved, so a browser downloaded by an older installation survives every
    later install (#686). Deleting either side of that would break an
    installation that can still be launched, so this reports the cache and
    leaves it exactly as it found it.

    The warning is bound to the cache state rather than to the process: the
    marker records the current revision together with the retained directory
    names, so a restart on the same cache stays quiet while the next patchright
    bump says its piece. A cache-local kernel lock makes the read, warning, and
    marker replacement one cross-process operation. Measuring the size comes
    after the marker check, so the walk over a multi-gigabyte cache happens once
    per state.

    Diagnostics, never a gate. Every failure is swallowed at debug level;
    browser setup must not depend on being able to measure a directory.
    """
    lock_fd: int | None = None
    try:
        configured = configure_browser_environment()
        if not configured.is_dir():
            return
        targets = _patchright_install_targets()
        current_revision = (targets or {}).get(_FULL_DIR_PREFIX)
        if current_revision is None:
            # Without the registry there is no way to tell the active revision
            # from a superseded one, and reporting the running browser as dead
            # weight is worse than reporting nothing.
            logger.debug("No patchright registry; skipping the browser cache report")
            return
        lock_fd = acquire_locked_fd(configured / _CACHE_REPORT_LOCK, exclusive=True)
        if lock_fd is None:
            # Another process is already reporting this shared cache. Its marker
            # will suppress later runs once it finishes.
            return
        retained = _retained_revision_dirs(configured, current_revision)
        if not retained:
            return
        signature = _cache_report_signature(current_revision, retained)
        marker = configured / _CACHE_REPORT_MARKER
        if _cache_report_is_current(marker, signature):
            return
        total = sum(_directory_size(entry) for entry in retained)
        logger.warning(
            "The managed browser cache at %s holds %s in browser revisions this "
            "server no longer launches: %s. Patchright keeps a revision for as "
            "long as any installed package still references it, and each "
            "installed server version that has run its browser installer leaves "
            "such a reference behind, so old revisions can stay indefinitely. "
            "To reclaim the "
            "space: stop every LinkedIn MCP Server instance, delete %s, and let "
            "the next launch download the current browser.",
            configured,
            _format_size(total),
            ", ".join(entry.name for entry in retained),
            configured,
        )
        try:
            secure_write_text(
                marker, json.dumps(signature, indent=2, sort_keys=True) + "\n"
            )
        except OSError:
            logger.debug("Could not record the browser cache report", exc_info=True)
    except (OSError, ProfileLeaseUnavailableError):
        logger.debug("Could not inventory the managed browser cache", exc_info=True)
    finally:
        if lock_fd is not None:
            _release_locked_fd(lock_fd)


def _schedule_retained_browser_revision_report() -> None:
    """Run the cache inventory off the event loop without delaying startup."""
    task = _state.cache_report_task
    if task is not None and not task.done():
        return
    _state.cache_report_task = asyncio.create_task(
        asyncio.to_thread(_report_retained_browser_revisions),
        name="browser-cache-report",
    )


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
            _schedule_retained_browser_revision_report()
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


async def _run_patchright_install(
    extra_arg: str, *, line_callback: Callable[[str], None] | None = None
) -> None:
    """Run one ``patchright install chromium`` stage with the given flag.

    The patchright registry lock serializes concurrent installs, so two
    processes reaching this at once queue on the same browsers path rather than
    corrupting it.

    Output is streamed line by line to ``logger.debug`` as it arrives, so a
    download that used to run behind a blank line reports its progress under
    ``--log-level DEBUG`` (#533). ``stderr`` is folded into ``stdout`` so the
    two interleave in the order patchright wrote them, and the collected lines
    become the failure message when the installer exits non-zero. A
    *line_callback* (``print`` for the CLI modes) receives each line too, so
    those modes show progress regardless of the log level.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "patchright",
        "install",
        "chromium",
        extra_arg,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    lines: deque[str] = deque()
    retained = 0
    try:
        async for raw in _installer_lines(proc.stdout):
            text = raw
            if not text:
                continue
            if line_callback is None:
                # The background path has nothing else to show it in. On the
                # CLI path the bar is the display, and logging each line too
                # would print every percentage permanently above it.
                logger.debug("patchright: %s", text)
            lines.append(text)
            retained += len(text)
            # A running total, because summing the deque per line is quadratic
            # in the output: measured, a multi-megabyte error body spent
            # seconds of CPU inside this bound while the point of it was to be
            # cheap. One line always survives, so a failure still quotes
            # something.
            while len(lines) > 1 and (
                len(lines) > _MAX_RETAINED_LINES or retained > _MAX_RETAINED_CHARS
            ):
                retained -= len(lines.popleft())
            if line_callback is not None:
                line_callback(text)
        await proc.wait()
    except BaseException:
        # Cancellation included, which is how shutdown and a failed peer reach
        # here. Reaps what this call started rather than leaving a process
        # behind for the lifetime of the server; the Node processes under it
        # survive, which ``_stop_installer`` records and this cannot fix.
        _stop_installer(proc)
        raise
    if proc.returncode != 0:
        raise BrowserSetupFailedError(
            "\n".join(lines) or "Patchright Chromium browser setup failed."
        )


def _finish(progress: Progress, task: TaskID | None) -> None:
    """Drive a task to completion, including one that never had a total."""
    if task is None:
        return
    active = next((t for t in progress.tasks if t.id == task), None)
    if active is None or active.finished:
        return
    total = active.total if active.total is not None else max(active.completed, 1)
    progress.update(task, total=total, completed=total)


def _parsed_total(found: re.Match[str] | None) -> int | None:
    """The download size a progress line names, or None if it names none.

    Patchright's current format always reports binary units and a plain decimal,
    but its error output can carry anything, and this runs inside a render
    callback where an exception costs the remaining retries. So an unknown unit
    or an unparseable number is treated as "not a progress line" and printed.
    """
    if found is None:
        return None
    unit = _BINARY_UNITS.get(found.group(3))
    if unit is None:
        return None
    try:
        return int(float(found.group(2)) * unit)
    except ValueError:
        return None


def _writes_to_the_terminal(stream: object) -> bool:
    """Whether this stream ends up on the screen. Never raises."""
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def _device_of(stream: object) -> tuple[int, int] | None:
    """Where this stream physically writes, or None if it cannot say.

    ``(0, 0)`` is *cannot say* rather than a device. Windows only fills these
    two in for a handle on disk: ``_Py_fstat_noraise`` zeroes the struct and
    then sets ``st_mode`` alone for ``FILE_TYPE_PIPE`` and ``FILE_TYPE_CHAR``
    (``Python/fileutils.c``, unchanged across the supported range). Two
    unrelated pipes would otherwise answer identically, and so would a console
    and a pipe, which is the pairing a redirect actually produces.
    """
    fileno = getattr(stream, "fileno", None)
    if not callable(fileno):
        return None
    try:
        status = os.fstat(fileno())
    except Exception:
        return None
    if status.st_dev == 0 and status.st_ino == 0:
        return None
    return (status.st_dev, status.st_ino)


def _same_destination(one: object, other: object) -> bool:
    """Whether two streams end up in the same place. Never raises.

    The device and inode rather than the descriptor number: stdout and stderr
    on a terminal are two descriptors on one device, and ``>log 2>&1`` is two
    descriptors on one file. Two *different* files answer differently, which is
    the case that must not be treated as a collision.

    One identity and one unknown is *not* the same place. Falling back to the
    terminal guess there would answer yes for a wrapper that hides its
    descriptor while writing to a second pane. This direction is the cheap one
    to get wrong: a false no draws a log record through the bar, a false yes
    takes it out of the destination the operator chose.
    """
    if one is other:
        return True
    first, second = _device_of(one), _device_of(other)
    if first is None and second is None:
        # Neither can name a device, which in practice means an in-memory
        # stream or a wrapper that hides the descriptor. Two streams that both
        # claim a terminal are the same terminal often enough to keep the old
        # answer here.
        return _writes_to_the_terminal(one) and _writes_to_the_terminal(other)
    # Identity against unknown is never equal, which `None` gives for free.
    #
    # Two *names* for one terminal are missed by this and stay missed: a
    # handler reopened through `/dev/tty` shares the screen with the pty slave
    # under it and carries a different inode (measured on macOS). Widening the
    # rule to same-device-and-both-a-terminal would close that and would also
    # merge two genuinely different ptys, which is the losing direction above.
    return first == second


@contextlib.contextmanager
def _log_handlers_follow_the_live_region(
    before: tuple[object, object],
    destination: object,
) -> Iterator[None]:
    """Let stream log handlers follow the streams rich redirects.

    rich swaps ``sys.stdout`` and ``sys.stderr`` for proxies while a live region
    is up, so anything written lands above the bar instead of through it. A
    ``StreamHandler`` built earlier holds the stream object it was given and
    never sees the swap, so a record emitted while the bar is live is drawn into
    the middle of it. Measured in a pty: without this the record and the bar
    ended up on the same physical line.

    *before* is what the two streams were just before the swap, which is what
    the handlers can be holding. Comparing against ``sys.__stderr__`` instead
    would only match when nothing else had wrapped it: and something usually
    has: pytest captures it, and the detached owner points it at the daemon log.

    *destination* is where the bar is drawn, and a handler only moves if it
    writes there too. Asking ``isatty`` instead was wrong in both directions
    once ``FORCE_COLOR`` or ``TTY_COMPATIBLE`` is set, which is how CI runners
    keep colour through a redirect: rich then draws into a file, so under
    ``>build.log 2>&1`` a handler that answers False stayed put and wrote its
    records into the middle of a rendered frame (measured), while with
    ``2>errors.log`` on a terminal a handler that answers True would be moved
    and its records taken out of the file the operator chose.

    Only handlers on those two streams move, so a file handler keeps its file.
    Restored on the way out, because the proxies stop working once the live
    region is gone.
    """
    old_stdout, old_stderr = before
    moved: list[tuple[logging.StreamHandler[Any], object, object]] = []
    for candidate in logging.getLogger().handlers:
        if not isinstance(candidate, logging.StreamHandler):
            continue
        # Re-bound because the isinstance narrowing leaves the stream type
        # unsolved, and `setStream` is declared against it.
        handler: logging.StreamHandler[Any] = candidate
        if handler.stream is old_stderr:
            proxy = sys.stderr
        elif handler.stream is old_stdout:
            proxy = sys.stdout
        else:
            continue
        # Only a handler writing where the bar is drawn can collide with it.
        # Both proxies write through the progress console, so moving one that
        # writes somewhere else would take its records out of the destination
        # the operator chose and put them where the bar is.
        if not _same_destination(handler.stream, destination):
            continue
        # `setStream` rather than the attribute: it takes the handler's lock
        # and flushes what is still buffered, so a record being emitted from
        # another thread is not split across the two streams. It answers None
        # when there was nothing to change, which is the case where rich left
        # the stream alone because something had already wrapped it.
        replaced = handler.setStream(proxy)
        if replaced is not None:
            moved.append((handler, replaced, proxy))
    try:
        yield
    finally:
        for handler, stream, proxy in moved:
            # Only if the handler is still where it was put. A host that
            # reconfigured logging during the download chose the newer stream,
            # and restoring over it would discard that choice and can leave
            # records pointed at a stream closed along with the old config.
            if handler.stream is proxy:
                handler.setStream(stream)


def _print_whatever_the_stream_takes(line: str) -> None:
    """Print a line, replacing anything the stream cannot encode."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode(encoding, "replace").decode(encoding), flush=True)
    except (OSError, ValueError):
        # A closed or broken stdout is not a reason to fail an install, and the
        # replacement attempt can meet the same closed pipe as the first.
        # ``ValueError`` because that is what a closed ``TextIOBase`` raises:
        # only a real pipe answers with ``BrokenPipeError``, and a stdout
        # already closed by the host answers "I/O operation on closed file".
        pass


def _bar_is_encodable() -> bool:
    """Whether the glyphs rich draws survive this stdout's encoding.

    rich does not consult the encoding before rendering, and it does not fall
    back either: the bar's U+2501 and the spinner's braille go straight into
    ``write`` and raise ``UnicodeEncodeError`` from inside the live region.
    That leaves the read loop, kills the installer, and reports an encoding
    error in place of a browser. ``PYTHONIOENCODING=ascii`` on a real terminal
    is enough to reach it, so the answer decides between the bar and the lines.
    """
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        _BAR_GLYPHS.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class _ConsoleThatGoesQuiet(Console):
    """A console that falls silent on a closed pipe instead of exiting.

    rich's default ``on_broken_pipe`` points ``sys.stdout`` at ``os.devnull``
    and raises ``SystemExit(1)``. Piping ``--install-browser`` into ``head``
    reaches it whenever the bar is drawn at all, which ``FORCE_COLOR=1`` or
    ``TTY_COMPATIBLE=1`` arranges for a redirected stdout: measured, exit code
    1 with an empty stderr, mid-download, with the archive half unpacked. Going
    quiet leaves the install running, which is the same answer the plain-line
    path already gives a stream that will not take a line.
    """

    def on_broken_pipe(self) -> None:
        self.quiet = True

    def _check_buffer(self) -> None:
        """Go quiet for the other ways a stream stops taking bytes, too.

        rich catches ``BrokenPipeError`` here and nothing else, so a stdout the
        host has closed (``ValueError``) or a pseudo-terminal whose other end
        detached (``OSError`` EIO) raises out of the bar's own refresh:
        measured against rich 15.0.0, both escape ``Console.print``. From there
        it leaves the read loop, kills the installer and reports a failed
        install for a browser already unpacked on disk. ``BrokenPipeError`` is
        an ``OSError``, so the base class has taken it before this sees it.
        """
        try:
            super()._check_buffer()
        except (OSError, ValueError):
            self.on_broken_pipe()
            del self._buffer[:]


@contextlib.contextmanager
def _cli_progress() -> Iterator[Callable[[str], None]]:
    """A live progress bar for a terminal, plain lines for anything else.

    Patchright writes to a pipe here, so it picks its newline-delimited
    progress: one line per ten percent, eleven lines per download. That reads
    fine in a log and poorly on a terminal, where eleven lines are one bar drawn
    eleven times. This reads the percentage back out and draws it once.

    Only for a terminal. Redirected output keeps the lines themselves, because a
    bar redrawn in place is noise in a file, and the background path never
    reaches here at all: its output goes to a debug log.

    Anything that does not parse as progress is printed unchanged, so a change
    in patchright's format costs the bar and never the message.
    """
    try:
        console = _ConsoleThatGoesQuiet(
            # rich's defaults give the three fields three different colours :
            # magenta, red, blue: which reads as noise beside a single-colour
            # bar. TransferSpeedColumn hardcodes its style name rather than
            # taking one, so the theme is the only place all three can be set.
            theme=Theme(
                {
                    "progress.percentage": _PROGRESS_STYLE,
                    "progress.data.speed": "grey58",
                    "progress.remaining": "grey58",
                }
            )
        )
        there_is_a_screen = console.is_terminal and not console.is_dumb_terminal
    except OSError:
        # A stream whose ``isatty`` raises instead of answering, which a
        # detached pseudo-terminal does with ``EIO``. rich guards that call for
        # ``ValueError`` alone (measured in 15.0.0), so an ``OSError`` comes
        # back out here. Not knowing whether there is a terminal is exactly the
        # plain-line case; raising would take the browser install with it.
        yield _print_whatever_the_stream_takes
        return

    if not there_is_a_screen or not _bar_is_encodable():
        # Whether there is a terminal at all is rich's judgement rather than
        # ``sys.stdout``'s, because where rich will not draw it does not fall
        # back either: ``Live.refresh`` renders through ``elif not
        # self._started``, which is to say once, on the way out. A dumb
        # terminal (``TERM=dumb`` or ``unknown``, common on CI consoles) passes
        # ``isatty`` and would then show nothing at all for the whole download,
        # which is the silence this change removes. What the stream can encode
        # is the one part rich does not decide, and it is asked separately.
        #
        # Not bare ``print`` on the way out: patchright draws its own bar with
        # U+25A0, and an ascii stream raises ``UnicodeEncodeError`` on it.
        # Raising here would leave the read loop, kill the installer, and
        # report an encoding error in place of the download. Reporting is never
        # worth that.
        yield _print_whatever_the_stream_takes
        return

    progress = Progress(
        SpinnerColumn(style=_PROGRESS_STYLE),
        TextColumn("[white]{task.description}"),
        BarColumn(
            bar_width=24,
            complete_style=_PROGRESS_STYLE,
            finished_style=_PROGRESS_STYLE,
            # The pulse is what shows while no percentage has arrived, which
            # is the whole of a chunked download and the whole of a failing
            # one. Left alone it shimmers in rich's magenta beside a blue bar.
            pulse_style=_PROGRESS_STYLE,
        ),
        TaskProgressColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        # rich's default 30s averaging window is deliberate here. Shortening it
        # to 10s makes a stall show up sooner, and costs far more than it buys:
        # rich drops samples older than the window before adding the next one,
        # so once one of patchright's ten-percent steps takes longer than the
        # window, a single sample is left, the elapsed span is zero, and both
        # the rate and the estimate become unavailable for the whole download.
        # Measured against the sample interval: at 6.6 MB/s a step takes 2.7s
        # and either window works; at 1.0 MB/s it takes 17.9s and only the 30s
        # window still reports anything. The slow connection is the one that
        # needs an estimate most.
        console=console,
        # rich redirects both streams while the bar is up, so that a direct
        # write lands above it rather than through it. Right for a stream that
        # shares the bar's screen and wrong for one that does not: under
        # ``2>errors.log`` rich would take a `sys.stderr.write` out of the file
        # the operator named and print it on the terminal instead, and leave a
        # handler built during the download pointed there afterwards. Same
        # question as the log handlers below, asked once, before rich swaps the
        # streams and the answer stops being available.
        redirect_stderr=_same_destination(sys.stderr, console.file),
    )
    described = "Installing browser"
    # Created before a single line arrives. Patchright can be silent for the
    # whole download: `npm_config_loglevel=warn` suppresses its announcements
    # and a chunked response suppresses its percentages, and it waits up to ten
    # minutes on the registry lock without saying anything either. A bar that
    # only appears once a line is recognised leaves all of those blank.
    task: TaskID | None = progress.add_task(described, total=None)

    def report(line: str) -> None:
        nonlocal task, described
        started = _PATCHRIGHT_DOWNLOAD.match(line)
        if started:
            # Patchright fetches ffmpeg after the browser, and retries a failed
            # download up to five times, announcing each attempt the same way.
            # A finished bar stays as a record; an unfinished one belonged to an
            # attempt that died, and leaving it would spin at 40% forever
            # beside its own retry.
            announced = started.group(1).split(" (")[0]
            placeholder = next(
                (t for t in progress.tasks if t.id == task and t.completed == 0),
                None,
            )
            if (
                task is not None
                and placeholder is not None
                and placeholder.total is None
            ):
                # Still the bar this context opened with. Name it rather than
                # stacking a second one beside it.
                progress.update(task, description=escape(announced))
                described = announced
                return
            if task is not None:
                active = next((t for t in progress.tasks if t.id == task), None)
                # Unfinished means the attempt died. Finished under the same
                # name means a retry after patchright reported 100% and then
                # failed to unpack, which it does on a corrupt archive: five of
                # those would leave four completed bars for one browser.
                same_artifact = active is not None and active.description == escape(
                    announced
                )
                if active is not None and (not active.finished or same_artifact):
                    with contextlib.suppress(KeyError):
                        progress.remove_task(task)
            # Escaped where it is used: the column renders the description as
            # markup, so an error body announcing "Downloading [/red] from …"
            # would raise out of the render callback and take patchright's
            # remaining retries with it.
            described = announced
            # Started here rather than at the first percentage. A mirror
            # serving the archive with ``Transfer-Encoding: chunked`` makes
            # patchright suppress progress altogether: its reporter is guarded
            # by ``if (!chunked && reportProgress)``: so a bar created on the
            # first percentage would never appear, and the download would run
            # behind the silence this change exists to remove. Without a total
            # rich pulses, and the total is filled in if a percentage arrives.
            task = progress.add_task(escape(described), total=None)
            return

        if _PATCHRIGHT_DONE.search(line):
            # The archive is in place. Without percentages this is the only
            # thing that says so, and a bar left pulsing would read as a
            # download that never ended.
            _finish(progress, task)
            progress.console.print(line, highlight=False, markup=False)
            return
        found = _PATCHRIGHT_PERCENT.search(line)
        total = _parsed_total(found)
        if total is None:
            # markup=False: the installer's text is data, not rich markup. An
            # error body containing "[/red]" would otherwise raise MarkupError
            # from inside the render callback, which aborts patchright's
            # remaining retries and reports rich instead of the download.
            progress.console.print(line, highlight=False, markup=False)
            return
        percent = int(found.group(1)) if found else 0
        active = next((t for t in progress.tasks if t.id == task), None)
        going_backwards = (
            active is not None
            and active.total is not None
            and int(total * percent / 100) < active.completed
        )
        if (
            going_backwards
            and task is not None
            and active is not None
            and (not active.finished or active.total == total)
        ):
            # An unfinished bar going backwards is the same artifact starting
            # over, which patchright does up to five times. Reuse it, or five
            # abandoned bars pile up for one download.
            #
            # A *finished* one going backwards to the same total is the same
            # thing seen without its announcements, which a raised npm log
            # level suppresses: ``logPolitely`` is silent from "warn" up while
            # the percentages keep coming. Patchright reports 100% before it
            # unpacks, so a corrupt archive finishes the bar and then retries,
            # and without this the failure ends under five completed bars. The
            # total is what tells the two apart: a retry re-downloads the same
            # bytes, and the next artifact is a different archive.
            progress.reset(task, total=total, description=active.description)
        elif task is None or going_backwards:
            # Backwards from a finished bar with a different total is the next
            # artifact, whose announcement never arrived for the same reason.
            task = progress.add_task(escape(described), total=total)
        elif task is not None:
            progress.update(task, total=total)
        # Bytes rather than percent, so the rate and the estimate have
        # something to divide. Both are as coarse as the ten-percent steps
        # patchright reports, which is honest: it is what the installer says.
        progress.update(task, completed=int(total * percent / 100))

    # Captured before entering, because that is what the handlers can be
    # holding; inside, rich has already replaced both. The console's own file
    # is read here for the same reason, though it unwraps the proxy either way.
    streams_before = (sys.stdout, sys.stderr)
    drawn_on = console.file
    with progress, _log_handlers_follow_the_live_region(streams_before, drawn_on):
        yield report
        # Reached only when the caller left without raising, so the install
        # succeeded. It can succeed in silence: patchright prints nothing at
        # all when every browser is already on disk and only the metadata was
        # stale, and the bar this context opens would then pulse under
        # "Browser installed." as its last frame.
        _finish(progress, task)


def _stop_installer(proc: asyncio.subprocess.Process) -> None:
    """Kill the installer process. Never raises.

    This reaches ``python -m patchright`` and nothing below it. Measured: the
    wrapper runs a Node CLI which runs a second Node process for the download,
    and after the wrapper is killed both were still alive, still downloading,
    and still refreshing the cache lock's heartbeat. So a cancelled install can
    keep the lock until it finishes on its own, and an install started in the
    meantime queues behind it.

    That is what ``main`` already did, and it is left alone deliberately.
    Reaching the whole tree means giving the installer its own session, which
    is a session the terminal's signals no longer reach; that was tried here
    and cost a crash on Windows, a race against the spawn, and a terminal left
    without its cursor. Closing this properly is worth its own change.
    """
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


def _safe_to_print(text: str) -> str:
    """Text with its terminal controls and its URL credentials taken out.

    Controls go first. A credential can be reassembled out of the fragments an
    escape sequence separates, so redacting before stripping would leave
    ``//us\x1b[0mer:pw@host`` unmatched and then hand the stripper a whole
    credential to put back together.

    What survives here is the download URL patchright echoes, with its userinfo
    and its query replaced. A credential the operator put in the *path* of
    ``PLAYWRIGHT_DOWNLOAD_HOST`` still prints: the path is also where the
    browser build lives, and blanking it would take the diagnostic with it.
    """
    text = _TERMINAL_CONTROLS.sub("", text)
    text = _CREDENTIALS_IN_URL.sub("//***@", text)
    return _QUERY_IN_URL.sub(r"\1?***", text)


async def _installer_lines(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """The installer's lines, with any quoted response body dropped.

    A body is the one part of this output a stranger writes, and patchright
    prints it verbatim, five times, once per retry. Dropping it here rather
    than defending against it downstream is what keeps the rest of this file
    honest: the escape sequences, the reflected credentials, the markup that
    would raise out of a render callback and the 400-digit sizes were all the
    same bytes arriving through the same quotes.

    Stateful across lines, because the body carries its own newlines and the
    marker that ends it can be thousands of lines further on. While a body is
    open every line is dropped whole, which also bounds what a mirror can make
    this process hold.
    """
    eliding = False
    carry = ""
    async for line in _split_installer_output(stream):
        if eliding:
            # The cut in ``_split_installer_output`` falls wherever the cap
            # does, including inside the marker, and half a marker on each side
            # of it would leave this open for the rest of the install: the URL,
            # the stack trace and every later retry dropped with the body.
            joined = carry + line
            found = _RESPONSE_BODY_CLOSED.search(joined)
            if found is None:
                carry = joined[-_CLOSER_CARRY:]
                continue
            eliding = False
            carry = ""
            # Resume at the marker: what follows it is patchright's own text.
            # Anything the match takes from the carry is marker, not body.
            line = joined[found.start() :]
        kept: list[str] = []
        # Scanning forward from ``pos`` rather than over the result: the marker
        # stays in what is kept, so a search from the front would find the same
        # opener again and never terminate.
        pos = 0
        while True:
            opened = _RESPONSE_BODY_OPENS.search(line, pos)
            if opened is None:
                kept.append(line[pos:])
                break
            kept.append(line[pos : opened.end()])
            kept.append(_OMITTED_BODY)
            found = _RESPONSE_BODY_CLOSED.search(line, opened.end())
            if found is None:
                eliding = True
                carry = line[-_CLOSER_CARRY:]
                break
            pos = found.start()
        # Yielded even when empty, so what a caller sees is still the line
        # structure the installer wrote.
        yield "".join(kept)


async def _split_installer_output(stream: asyncio.StreamReader) -> AsyncIterator[str]:
    """The installer's output, split into lines, with no limit on line length.

    Deliberately not ``async for line in stream``. That reads through
    ``readline``, which raises ``ValueError`` once a single line passes the
    reader's 64 KiB limit: mid-download, out of the loop, and with the child
    still running and unreaped. Patchright puts an entire non-200 response body
    into one error line, so a captive portal or a mirror answering with minified
    HTML reaches that limit, and the server would report an asyncio error
    instead of the download failure. Measured: a 70 000-byte line raises
    ``Separator is found, but chunk is longer than limit`` and leaves the child
    alive.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        buffer += decoder.decode(chunk)
        # Sanitised here, on the buffer, while a credential is still whole. Per
        # fragment it would be too late: the cut below is a split point like
        # any other, and half a URL matches no pattern. Together with the tail
        # held back, any userinfo shorter than that tail is either redacted
        # before anything is emitted or still sitting in the buffer when the
        # rest of it arrives.
        buffer = _safe_to_print(buffer)
        while True:
            end = buffer.find("\n")
            if 0 <= end <= _MAX_LINE_CHARS:
                # rstrip here, where the line genuinely ends. A forced fragment
                # below ends where the cap fell, and trimming there would eat
                # whitespace the installer wrote.
                line, buffer = buffer[:end].rstrip(), buffer[end + 1 :]
                yield line
                continue
            if len(buffer) <= _MAX_LINE_CHARS:
                break
            # Past the cap with no newline in reach: emit a bounded piece and
            # keep going. Checking the newline first is what a size check alone
            # got wrong: two reads under the read size could still meet as one
            # fragment of twice the cap.
            #
            # A credential whole in the buffer was redacted above, before any
            # of this. One whose "@" has not been read yet is split by the cut
            # and prints in halves, and no window held back here changes that:
            # the buffer is over the cap by definition, so holding the opener
            # back only moves the same fragment one iteration later. Which no
            # longer has an author: the only output patchright produced without
            # newlines was the quoted response body, and that is dropped.
            #
            # A URL cut in half here loses its scheme with the near fragment,
            # and the query the far one completes then matches no pattern: a
            # token split across two reads at the cut printed whole, measured.
            # Cutting at the last space instead would keep tokens together and
            # cannot be done from here: the opener ends in one, so preferring a
            # space splits *that* marker instead, systematically rather than by
            # coincidence, and the body it opens then prints in full. Both ends
            # want the same thing, which is a splitter that knows where the
            # markers are, and that is its own change.
            yield buffer[:_MAX_LINE_CHARS]
            buffer = buffer[_MAX_LINE_CHARS:]
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip()


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


async def _run_browser_setup(
    *, line_callback: Callable[[str], None] | None = None
) -> None:
    """Install full Chrome for Testing, in one stage.

    The two-stage shell-first arrangement existed to make the headless path
    usable sooner. Nothing launches the shell now, so staging it would download
    92 MiB nobody runs.

    Worth being straight about the effect on download size, because it cuts both
    ways. Measured against the CDN for 148.0.7778.96 mac-arm64: this is 170 MiB,
    against 263 MiB for anyone who previously ended up needing the full browser,
    and against 92 MiB for the default headless user who only ever fetched the
    shell. The second comparison is the one users will notice.

    Those three figures are one revision's *and one platform's*, not a
    constant. The bundled browser moves with the lockfile and is past 148 now,
    and the sizes differ by platform as well: the arm64 container does not get
    Chrome for Testing at all, it gets Playwright's own Chromium build. What
    the argument needs is only that the full browser is substantially larger
    than the shell everywhere, which holds; quoting these particular numbers
    anywhere user-facing means re-measuring them for the platform in question.
    """
    browser_dir = configure_browser_environment()
    secure_mkdir(browser_dir)

    # Timed here, right where the download runs, rather than at the state
    # reconciliation that flips this to READY: that reconciliation happens on
    # the next tool or auth call, which can arrive minutes after the install
    # actually finished, and would report the idle gap as setup time.
    started = time.monotonic()
    await _run_patchright_install("--no-shell", line_callback=line_callback)
    _write_install_metadata(
        browser_dir,
        {_SHELL_DIR_PREFIX: False, _FULL_DIR_PREFIX: True},
    )
    # After the metadata, which is what makes the browser count as installed.
    # Before it, a failing write would follow "setup completed" with a failure.
    logger.info(
        "Patchright Chromium browser setup completed in %.0fs",
        time.monotonic() - started,
    )
    # After the installer, because that is the moment a bump has just added a
    # revision beside the old one and patchright has already had its chance to
    # collect what it could. The inventory is diagnostic, so it runs separately
    # and does not delay browser readiness.
    _schedule_retained_browser_revision_report()


async def _ensure_browser_installed(
    *, line_callback: Callable[[str], None] | None = None
) -> None:
    """Install the browser on demand. A no-op once it is present."""
    if browser_ready():
        return
    await _run_browser_setup(line_callback=line_callback)


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
    # while two of these three modes needed only the much smaller shell; now
    # they all want the full browser, so it is the whole download spent on
    # nothing -- and for someone whose network cannot reach the CDN, it is the
    # difference between signing in and not.
    if _uses_custom_chrome():
        return
    if browser_ready():
        _report_retained_browser_revisions()
        return
    # Through the helper, not ``print``: these three sit on the same stdout the
    # bar does, so ``--install-browser | head`` reaches them with the pipe
    # already gone. Measured: a *successful* install then ended in a
    # ``BrokenPipeError`` traceback, and a failed one raised it in place of the
    # ``BrowserSetupFailedError`` that says what went wrong. The helper also
    # carries the encoding fallback, which the cross mark below needs on an
    # ascii terminal for the same reason.
    _print_whatever_the_stream_takes("   Installing Patchright Chromium browser...")
    try:
        with _cli_progress() as report:
            asyncio.run(_ensure_browser_installed(line_callback=report))
    except Exception as exc:
        _print_whatever_the_stream_takes(f"   ❌ Browser installation failed: {exc}")
        raise
    _print_whatever_the_stream_takes("   Browser installed.")


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
        "No valid LinkedIn session is available in Docker. Create one with "
        "the explicit --login --login-viewer Docker command, or run --login "
        "on the host, then retry this tool."
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
