"""The harness core: account boundary, watcher, host stub, and row H-R1.

**Account boundary.** Spawned actors use the account's *real* daemon state
root, because ``daemon_descriptor._account_home`` ignores ``HOME`` on purpose
and the owner is started by production code that has nowhere to inject a
redirection. What keeps them off the user's state is the key: the daemon
directory is a hash of the auth root, and every row's auth root is a fresh
temporary directory, so cleanup has one exact target (``daemon_dir``). This is
the policy of ``real_state_root`` in ``tests/test_daemon_election.py``.

The one auth root no row may ever use is the user's, ``~/.linkedin-mcp``.
``claim_account`` refuses it, and anything that contains it or sits inside it,
before a file is read, a browser launched or a process spawned. It judges the
path as written first, then the filesystem's own identity of every directory
involved, which is what catches a case alias on a case-insensitive volume or a
link. An account whose home cannot be determined is refused, not assumed
harmless. ``HOME`` is deliberately not redirected for the actors: on Linux the
bundled browser reads the trusted test CA from ``~/.pki/nssdb``, so a different
home would be a different trust store.

**Owner cleanup acts only on the owner this row identified.** Its pid, create
time, instance and auth root are recorded when the row finds it, and the
``psutil.Process`` taken then is the only handle cleanup ever signals. A
descriptor naming anything else, or an identity that no longer holds, is
refused and the row's daemon directory is kept as evidence.

**Host stub.** A real MCP client over stdio, spawning the server from this
virtual environment the way a host does. It is not ``fastmcp``'s own stdio
transport: that one starts the server in a new session and, when the client
leaves, closes stdin and then escalates to signals after two seconds (measured
again under FastMCP 4, whose default also keeps the server running past the
client unless ``keep_alive`` is off). A host
quit is stdin EOF and a wait, and a server closing a browser takes longer than
two seconds, so that transport would turn every host quit into a kill. This one
starts the server in the harness's own process group, where the child of a host
that does not detach it lands, and on quit closes stdin and waits. Not leading
a group matters: a Direct server that leads one hands its guardian that group
to signal (``process_tree.start_browser_guardian``), a different topology.

**Row H-R1** (normal start, one host, local storage, bundled browser): stage a
signed-in session, start the watcher, initialize, call one read tool, quit the
host, and wait for the server, the owner and the profile's browser to be gone.
In daemon mode the owner leaves through its own idle exit. Then, outside the
row's interval, one short Direct session on the same profile shows whether the
origin still accepts the staged session. Before and after, the R17 snapshot.

K1 here is a **same-revision Direct reference**: the server of this checkout
with the daemon off. The plan's **frozen K1** and **K2** run the pinned baseline
instead (``baseline.Runtime``), with its own interpreter, staging and browser,
and are labelled with its short SHA; the two K1 columns are kept apart.

**Row H-R12** (custom browser): the daemon enabled and ``CHROME_PATH`` set to
the runtime's own bundled Chromium, so the browser is the same binary and only
the setting differs. The candidate must show no coordination effect (no owner,
no forwarding, no daemon state) and the O1/O4 of the frozen Direct run with the
same setting; the baseline, in K2, must be caught coordinating
(``k2_r12_verdict``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
import mcp.types as mcp_types
import psutil
from anyio.streams.text import TextReceiveStream
from fastmcp import Client
from fastmcp.client.transports.base import (
    ClientTransport,
    SessionKwargs,
    TransportOptions,
)
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from typing_extensions import Unpack

from differential.baseline import (
    BaselineRefused,
    Runtime,
    bundled_executable,
    checkout_refusal,
    frozen_identity,
    interpreter_failures,
    stage_frozen_session,
)
from differential.events import EventLog, read_jsonl
from differential.session import (
    RETAINED,
    UNCERTAIN,
    ProfileSnapshot,
    r17_outcome,
    snapshot,
    stage_signed_in_session,
    write_synthetic_cookie_file,
)
from differential.signals import UNKNOWN as O2_UNKNOWN
from differential.signals import UNOBSERVED as O2_UNOBSERVED
from differential.signals import VIOLATED as O2_VIOLATED
from differential.signals import (
    Canaries,
    O2Result,
    ProcessHistory,
    SignalOracle,
    classes_direct_would_not_send,
    derive_o2,
)
from differential.synthetic_origin import (
    POST_MARKER,
    EgressProxy,
    SyntheticOrigin,
)
from differential.watcher import (
    LAUNCHER_ENV,
    OWNER_MODULE,
    USER_DATA_DIR_FLAG,
    another_user,
    canonical_user_data_dir,
    harness_user,
    invoked_module,
    possible_browser,
    process_user,
)
from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.session_state import portable_cookie_path

REAL_AUTH_ROOT_NAME = ".linkedin-mcp"

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "mcp-server-linkedin"

#: The browser cache this run was started with. Read at import, because the
#: suite's ``reset_bootstrap_for_testing`` deletes the variable per test.
_INHERITED_BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")

WATCHER_SCRIPT = Path(__file__).with_name("watcher.py")

ROW_H_R1 = "H-R1"
#: R12, a custom browser: ``CHROME_PATH`` set to the runtime's own bundled
#: Chromium, so the browser is the same binary and only the setting differs.
ROW_H_R12 = "H-R12"
DIRECT_REFERENCE = "same-revision Direct reference"
#: The read tool H-R1 calls. ``get_feed`` because the product already has to
#: load ``/feed/`` to sign in, so the synthetic origin serves one page for both
#: and the extractor reads innerText under ``<main>`` without any selector tied
#: to LinkedIn's layout. See ``synthetic_origin._FEED_PAGE``.
READ_TOOL = "get_feed"
READ_TOOL_ARGUMENTS = {"num_posts": 1}

#: The owner leaves through its own idle exit once the host has quit, so the
#: daemon row ends through the product's path rather than a signal. Set for
#: both modes, so the two configurations differ only in DAEMON_ENABLED. Not
#: shorter: the owner's quiet period starts when it publishes, so a value
#: below the frontend's time from election to first call would retire the
#: owner before the row's one call reached it.
IDLE_TIMEOUT_SECONDS = 20.0

#: The largest wall-clock gap between two watcher samples a row accepts. The
#: maximum accepted gap is an observation-quality budget, not proof that every
#: browser lifetime is sampled. Record the actual gaps; an overlap wholly
#: between samples remains outside this oracle's resolution. Twenty times the
#: target interval, so a busy runner passes and a stalled watcher does not.
MAX_WATCHER_GAP_SECONDS = 1.0

_HOST_EXIT_SECONDS = 90.0
_OWNER_EXIT_SLACK_SECONDS = 90.0
_BROWSER_GONE_SECONDS = 60.0
_OWNER_KILL_WAIT_SECONDS = 15.0
_INIT_SECONDS = 180.0
_CALL_SECONDS = 240.0
_STDERR_EOF_SECONDS = 10.0

_FORWARDING_LINE = "Forwarding to the shared browser owner"
_IDLE_EXIT_LINE = "Nothing has needed the browser in"
#: ``__PYVENV_LAUNCHER__`` included: a framework build takes its venv from it,
#: so the harness's own value would hand a baseline actor the candidate's venv.
_FOREIGN_CODE = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", LAUNCHER_ENV}
)


class ContainmentError(RuntimeError):
    """The configured auth root is the user's own, or would reach it."""


class EvidenceRefused(RuntimeError):
    """The runtime is not the checkout this row claims to measure."""


def default_browsers_path() -> Path:
    """Where ``patchright install`` put the browser, as the driver computes it."""
    if _INHERITED_BROWSERS_PATH:
        return Path(_INHERITED_BROWSERS_PATH)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / (
        "ms-playwright"
    )


# --- Account boundary --------------------------------------------------------


def account_homes() -> tuple[Path, ...]:
    """Both homes that can name the user's auth root, or a refusal.

    The environment's (``Path.home()``) and the operating system account's,
    which the daemon keys on and which ignores ``HOME``. Not knowing the second
    is not evidence that nothing is there to protect.
    """
    try:
        account = daemon_descriptor._account_home()
    except Exception as exc:
        raise ContainmentError(
            f"the account's home directory could not be determined "
            f"({type(exc).__name__}), so the auth root it protects cannot be "
            f"excluded; refusing"
        ) from exc
    return (Path.home(), Path(account))


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Different drives on Windows: neither contains the other.
        return False


def _identity(path: str) -> tuple[int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _chain(path: str) -> list[tuple[int, int]]:
    """The identities of *path* and of every ancestor of it that exists.

    Along the path as written and along its resolved form, since a link's
    written ancestors are not the ancestors of the directory it reaches.
    """
    identities = []
    for spelling in dict.fromkeys((path, os.path.realpath(path))):
        current = spelling
        while True:
            identity = _identity(current)
            if identity is not None and identity not in identities:
                identities.append(identity)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return identities


@dataclass(frozen=True)
class ActorAccount:
    """A profile the harness may hand to actors. Built only by ``claim_account``."""

    profile: Path

    @property
    def auth_root(self) -> Path:
        return self.profile.parent

    @property
    def browser_key(self) -> str:
        """The profile as the watcher spells it."""
        return canonical_user_data_dir(str(self.profile))


def _refuse_overlap(profile: Path, auth_root: str, reals: list[str]) -> None:
    """Refuse *auth_root* if it is, holds or sits inside any of *reals*.

    As a string first, so a root named inside the real one is refused without a
    filesystem call beneath it; then by the filesystem's identity (device and
    inode) of the auth root and each of its ancestors, written and resolved,
    against the real root and each of its ancestors. Identity is what the
    filesystem itself says is the same directory, so a case alias on a
    case-insensitive volume and a link are both caught, and two names a
    case-sensitive volume keeps apart stay apart. The auth root has to exist:
    an identity that cannot be read cannot be judged.
    """
    written = os.path.normcase(auth_root)
    for real in reals:
        protected = os.path.normcase(real)
        if _within(written, protected) or _within(protected, written):
            raise ContainmentError(
                f"refusing profile {profile}: its auth root {auth_root} "
                f"overlaps the account's own {real}"
            )
    candidate = _identity(auth_root)
    if candidate is None:
        raise ContainmentError(
            f"refusing profile {profile}: its auth root {auth_root} does not "
            f"exist, so its identity cannot be judged"
        )
    candidate_chain = _chain(auth_root)
    for real in reals:
        real_identity = _identity(real)
        if real_identity is not None and real_identity in candidate_chain:
            raise ContainmentError(
                f"refusing profile {profile}: its auth root {auth_root} is the "
                f"account's own {real} or inside it, under another name"
            )
        if candidate in _chain(real):
            raise ContainmentError(
                f"refusing profile {profile}: its auth root {auth_root} contains "
                f"the account's own {real}"
            )


def claim_account(profile: Path) -> ActorAccount:
    """Refuse the user's own auth root, before anything touches the profile.

    Refused in both directions: an auth root at or inside ``~/.linkedin-mcp``,
    and one at or above it, such as the home directory itself.

    Two auth roots are judged, completely: the parent of the profile as
    written, and the parent of the profile once the whole path is resolved.
    The second is the one every product path acts on, since they resolve the
    configured profile before taking its parent, and it differs from the first
    exactly when the profile itself is a link. The account returned carries the
    resolved profile, which is the path that was checked.
    """
    reals = [
        os.path.join(os.path.abspath(home), REAL_AUTH_ROOT_NAME)
        for home in account_homes()
    ]
    raw = os.path.abspath(os.path.expanduser(profile))
    # As written first, and before any filesystem call on the profile itself.
    _refuse_overlap(profile, os.path.dirname(raw), reals)
    resolved = os.path.realpath(raw)
    _refuse_overlap(profile, os.path.dirname(resolved), reals)
    return ActorAccount(Path(resolved))


def actor_environment(
    account: ActorAccount,
    proxy_url: str,
    *,
    daemon: bool,
    browsers: Path,
    chrome_path: str | None = None,
) -> dict[str, str]:
    """The server's environment: this one, minus every setting, plus the row's."""
    settings = {
        value
        for name, value in vars(EnvironmentKeys).items()
        if not name.startswith("_") and isinstance(value, str)
    }
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in settings
        and not key.startswith("LINKEDIN")
        # What could put another checkout's code on the actors' path: a
        # frozen row's interpreter must import its own package and nothing else.
        and key not in _FOREIGN_CODE
    }
    env.update(
        {
            EnvironmentKeys.USER_DATA_DIR: str(account.profile),
            EnvironmentKeys.PROXY_SERVER: proxy_url,
            EnvironmentKeys.DAEMON_ENABLED: "true" if daemon else "false",
            EnvironmentKeys.HEADLESS: "true",
            EnvironmentKeys.LOG_LEVEL: "INFO",
            EnvironmentKeys.BROWSER_IDLE_TIMEOUT: str(IDLE_TIMEOUT_SECONDS),
            "LINKEDIN_MCP_CHECK_FOR_UPDATES": "off",
            "PLAYWRIGHT_BROWSERS_PATH": str(browsers),
        }
    )
    if chrome_path is not None:
        env[EnvironmentKeys.CHROME_PATH] = chrome_path
    return env


def server_command() -> list[str]:
    return [sys.executable, "-m", "linkedin_mcp_server"]


def candidate_runtime() -> Runtime:
    """This checkout, run from the harness's own interpreter."""
    browsers = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default_browsers_path()
    )
    return Runtime(sys.executable, REPO_ROOT, browsers)


# --- Runtime identity ----------------------------------------------------------


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def row_identity(repo: Path = REPO_ROOT) -> dict[str, Any]:
    """What the actors will import and run, recorded before they start.

    Every actor is started from ``sys.executable``; the owner by production
    code, which reuses it. So the interpreter, the installed package's
    ``direct_url.json``, and the checkout it points at are the code under test.
    """
    import sysconfig
    from importlib.metadata import distributions

    # The install in this interpreter's own site-packages, which is what an
    # actor started from ``sys.executable`` imports. Not whatever the lookup
    # meets first on this process's path: a build left in the checkout, say,
    # an ``*.egg-info`` with no ``direct_url.json``.
    site = sysconfig.get_paths()["purelib"]
    installed = list(distributions(name=PACKAGE, path=[site]))
    direct_url = None
    if len(installed) == 1:
        try:
            raw = installed[0].read_text("direct_url.json")
            direct_url = json.loads(raw) if raw else None
        except ValueError:
            direct_url = None
    head = _git(repo, "rev-parse", "HEAD")
    # ``--no-optional-locks``: reading the state must not write the index.
    porcelain = _git(repo, "--no-optional-locks", "status", "--porcelain")
    lock = repo / "uv.lock"
    return {
        "reference": DIRECT_REFERENCE,
        "sys_executable": sys.executable,
        "site_packages": site,
        "installed_distributions": len(installed),
        "direct_url": direct_url,
        "checkout": str(repo),
        "head": head.strip() if head else None,
        "porcelain_empty": porcelain == "" if porcelain is not None else None,
        "dirty_paths": (porcelain or "").splitlines()[:50],
        "uv_lock_sha256": (
            hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None
        ),
    }


def evidence_refusal(identity: dict[str, Any], *, ci: bool) -> str | None:
    """Why this runtime cannot stand for the checkout, or None."""
    direct_url = identity.get("direct_url") or {}
    url = direct_url.get("url") if isinstance(direct_url, dict) else None
    editable = isinstance(direct_url, dict) and (direct_url.get("dir_info") or {}).get(
        "editable"
    )
    installed_from: str | None = None
    if isinstance(url, str) and url.startswith("file:"):
        installed_from = url2pathname(urlparse(url).path)
    checkout = identity.get("checkout")
    if not editable or installed_from is None or checkout is None:
        return (
            f"{PACKAGE} is not an editable install of a local checkout "
            f"({direct_url!r}); the row would run code this record cannot name"
        )
    try:
        same = os.path.samefile(installed_from, checkout)
    except OSError:
        same = False
    if not same:
        return (
            f"{PACKAGE} is installed from {installed_from}, not from the checkout "
            f"{checkout} whose revision this row records"
        )
    if identity.get("head") is None:
        return "git could not name the checkout's HEAD"
    if ci and identity.get("porcelain_empty") is not True:
        return (
            f"the checkout is not clean, so HEAD does not describe the code the "
            f"actors import: {identity.get('dirty_paths')}"
        )
    return None


def frozen_refusal(identity: dict[str, Any], runtime: Runtime) -> str | None:
    """Why a frozen runtime is not its pin, installed from its own checkout."""
    assert runtime.pinned is not None
    return checkout_refusal(identity, runtime.pinned) or evidence_refusal(
        identity, ci=True
    )


# --- Watcher -----------------------------------------------------------------


class Watcher:
    """The watcher process, and the events it wrote once it has stopped."""

    def __init__(
        self,
        directory: Path,
        log: EventLog,
        *,
        experiment: str,
        row: str,
        browser_exe: str | None = None,
        browser_dir: Path | None = None,
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.browser_exe = browser_exe
        self.browser_dir = browser_dir
        self.out = directory / "watcher.jsonl"
        self.stop_file = directory / "watcher.stop"
        self.stderr = directory / "watcher.stderr"
        self._log = log
        self._experiment = experiment
        self._row = row
        self._process: subprocess.Popen[Any] | None = None

    def start(self, *, ready_seconds: float = 15.0) -> None:
        command = [
            sys.executable,
            str(WATCHER_SCRIPT),
            "--out",
            str(self.out),
            "--stop",
            str(self.stop_file),
            "--run",
            self._log.run,
            "--experiment",
            self._experiment,
            "--row",
            self._row,
            "--platform",
            self._log.platform,
            "--root-pid",
            str(os.getpid()),
        ]
        if self.browser_exe:
            command += ["--browser-exe", self.browser_exe]
        if self.browser_dir is not None:
            command += ["--browser-dir", str(self.browser_dir)]
        detach: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        with self.stderr.open("wb") as err:
            process: subprocess.Popen[Any] = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=err, stderr=err, **detach
            )
        self._process = process
        # The first sample is the baseline: a process already running then is
        # never reported as started. Waiting for it means every actor the row
        # starts afterwards is reported as one.
        deadline = time.monotonic() + ready_seconds
        while time.monotonic() < deadline:
            if any(r.get("kind") == "watcher.ready" for r in read_jsonl(self.out)):
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        # Detached from the row, so nothing else would end it before its own
        # deadline: a watcher that never took its baseline is stopped here.
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=10)
        raise RuntimeError(
            f"the watcher did not take its baseline sample: "
            f"{self.stderr.read_text(errors='replace')[-2000:]}"
        )

    def observed(self) -> list[dict[str, Any]]:
        """What the watcher has written so far, read while it still runs."""
        return read_jsonl(self.out)

    def stop(self) -> dict[str, Any] | None:
        """Stop sampling, copy its events into the log, return its summary."""
        if self._process is None:
            return None
        self.stop_file.touch()
        try:
            self._process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=15)
        records = read_jsonl(self.out)
        self._log.extend(records)
        summaries = [r for r in records if r.get("kind") == "watcher.summary"]
        return summaries[-1] if summaries else None


def watcher_failures(
    summary: dict[str, Any] | None,
    *,
    actors_began: float,
    actors_ended: float,
    max_gap: float = MAX_WATCHER_GAP_SECONDS,
) -> list[str]:
    """Why this observation cannot carry O1, or nothing when it can."""
    if not summary:
        return ["the watcher wrote no summary"]
    failures = []
    if summary.get("stopped_by") != "stop file":
        failures.append(
            f"the watcher stopped by {summary.get('stopped_by')!r}, not at the "
            f"harness's request"
        )
    start, end = summary.get("observation_start"), summary.get("observation_end")
    if not isinstance(start, (int, float)) or start > actors_began:
        failures.append("the watcher's observation began after the actors started")
    if not isinstance(end, (int, float)) or end < actors_ended:
        failures.append("the watcher's observation ended before the actors were gone")
    gap = summary.get("max_gap_seconds")
    if not isinstance(gap, (int, float)) or gap > max_gap:
        failures.append(
            f"the watcher's largest gap between samples was {gap}s, over the "
            f"{max_gap}s this row accepts"
        )
    # Only an actor that could have been a browser root: one whose executable
    # could not be read, or is the row's browser. Every failed read stays in
    # the summary's ``read_failures`` either way.
    unread = summary.get("relevant_read_failures") or []
    if unread:
        failures.append(
            f"the watcher could not identify {len(unread)} row actors as "
            f"anything but a possible browser: {unread[:5]}"
        )
    return failures


# --- Host stub ---------------------------------------------------------------


class HostQuitTransport(ClientTransport):
    """Stdio to a real server, where quitting is stdin EOF and a wait."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path,
        on_stderr: Callable[[str], None],
        exit_seconds: float = _HOST_EXIT_SECONDS,
    ) -> None:
        self.command = list(command)
        self.env = env
        self.cwd = cwd
        self.on_stderr = on_stderr
        self.exit_seconds = exit_seconds
        self.process: anyio.abc.Process | None = None
        self.pid: int | None = None
        self.quit_done = False
        self.alive_before_quit: bool | None = None
        self.stdin_closed: bool | None = None
        self.stdin_close_error: str | None = None
        self.exited_on_quit: bool | None = None
        self.quit_seconds: float | None = None
        self.killed_by_harness = False
        self.stderr_closed: bool | None = None
        self._stderr_eof = anyio.Event()

    async def _pump_stderr(self, process: anyio.abc.Process) -> None:
        assert process.stderr is not None
        buffer = ""
        try:
            async for chunk in TextReceiveStream(process.stderr, errors="replace"):
                lines = (buffer + chunk).split("\n")
                buffer = lines.pop()
                for line in lines:
                    self.on_stderr(line.rstrip("\r"))
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            pass
        finally:
            if buffer:
                self.on_stderr(buffer.rstrip("\r"))
            self._stderr_eof.set()

    async def host_quit(self) -> None:
        """What a host does on quit: close the server's stdin, then wait."""
        process = self.process
        assert process is not None and process.stdin is not None
        self.alive_before_quit = process.returncode is None
        began = time.monotonic()
        try:
            await process.stdin.aclose()
            self.stdin_closed = True
        except Exception as exc:  # noqa: BLE001 - recorded, and judged by the row
            self.stdin_closed = False
            self.stdin_close_error = f"{type(exc).__name__}: {exc}"
        with anyio.move_on_after(self.exit_seconds):
            await process.wait()
        self.quit_seconds = round(time.monotonic() - began, 3)
        self.exited_on_quit = process.returncode is not None
        self.quit_done = True
        # A grandchild that inherited stderr keeps it open past the exit, so
        # this wait is bounded and its result is evidence, not a requirement.
        with anyio.move_on_after(_STDERR_EOF_SECONDS):
            await self._stderr_eof.wait()
        self.stderr_closed = self._stderr_eof.is_set()

    async def _stop(self, process: anyio.abc.Process) -> None:
        """Cleanup only: a server still running when the stub leaves."""
        if process.returncode is not None:
            return
        self.killed_by_harness = True
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
        with anyio.move_on_after(15):
            await process.wait()

    @contextlib.asynccontextmanager
    async def connect_session(
        self,
        *,
        transport_options: TransportOptions | None = None,
        **session_kwargs: Unpack[SessionKwargs],
    ) -> AsyncIterator[ClientSession]:
        options = transport_options or TransportOptions()
        process = await anyio.open_process(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            cwd=str(self.cwd),
        )
        self.process = process
        self.pid = process.pid
        read_send, read_receive = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        write_send, write_receive = anyio.create_memory_object_stream[SessionMessage](0)

        async def pump_stdout() -> None:
            assert process.stdout is not None
            buffer = ""
            async with read_send:
                with contextlib.suppress(
                    anyio.ClosedResourceError, anyio.BrokenResourceError
                ):
                    async for chunk in TextReceiveStream(process.stdout):
                        lines = (buffer + chunk).split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            if not line.strip():
                                continue
                            # Parsed and written as the SDK's own stdio
                            # client does (mcp/client/stdio.py).
                            try:
                                message = (
                                    mcp_types.jsonrpc_message_adapter.validate_json(
                                        line, by_name=False
                                    )
                                )
                            except Exception as exc:
                                await read_send.send(exc)
                                continue
                            await read_send.send(SessionMessage(message))

        async def pump_stdin() -> None:
            assert process.stdin is not None
            async with write_receive:
                with contextlib.suppress(
                    anyio.ClosedResourceError, anyio.BrokenResourceError
                ):
                    async for outgoing in write_receive:
                        data = outgoing.message.model_dump_json(
                            by_alias=True, exclude_unset=True
                        )
                        await process.stdin.send((data + "\n").encode())

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pump_stdout)
            tasks.start_soon(pump_stdin)
            tasks.start_soon(self._pump_stderr, process)
            try:
                async with options.session_class(
                    read_receive, write_send, **session_kwargs
                ) as session:
                    yield session
            finally:
                with anyio.CancelScope(shield=True):
                    await self._stop(process)
                tasks.cancel_scope.cancel()


def tool_summary(result: mcp_types.CallToolResult) -> dict[str, Any]:
    """What the host saw from the call, without copying the whole result."""
    texts = [
        block.text
        for block in result.content
        if isinstance(block, mcp_types.TextContent)
    ]
    structured = result.structured_content
    if not isinstance(structured, dict):
        structured = {}
    if isinstance(structured.get("result"), dict):
        structured = structured["result"]
    sections = structured.get("sections")
    feed = sections.get("feed") if isinstance(sections, dict) else None
    return {
        "is_error": bool(result.is_error),
        "sections": sorted(sections) if isinstance(sections, dict) else [],
        "section_errors": sorted(structured.get("section_errors") or {}),
        "read_the_post": isinstance(feed, str) and POST_MARKER in feed,
        "text": "\n".join(texts)[:2000],
    }


@dataclass
class HostSession:
    stderr: list[str] = field(default_factory=list)
    #: Everything the user saw, in order: stderr lines and the tool's text.
    user_lines: list[str] = field(default_factory=list)
    pid: int | None = None
    tool: dict[str, Any] | None = None
    alive_before_quit: bool | None = None
    stdin_closed: bool | None = None
    stdin_close_error: str | None = None
    exited_on_quit: bool | None = None
    exit_code: int | None = None
    quit_seconds: float | None = None
    stderr_closed: bool | None = None
    killed_by_harness: bool = False
    #: A failure before the quit completed: initialize, the call, the hook.
    error: str | None = None
    #: A failure while the client unwound after a completed quit. Evidence only.
    teardown_error: str | None = None
    #: H-R6: the call made after the owner was killed, and how it failed.
    second_tool: dict[str, Any] | None = None
    second_error: str | None = None


async def run_host_session(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    on_stderr: Callable[[str], None],
    after_call: Callable[[], Awaitable[None]] | None = None,
    tool: str = READ_TOOL,
    arguments: dict[str, Any] | None = None,
    started: Callable[[int], None] | None = None,
    second_call: bool = False,
) -> HostSession:
    """Initialize, call the read tool once, then quit the way a host does.

    *started* is told the server's pid as soon as it runs. With *second_call*
    the tool is called once more after *after_call*, which is how H-R6 sees the
    frontend recover from a killed owner; its failure is recorded apart and
    does not stop the host from quitting.
    """
    session = HostSession()

    def remember(line: str) -> None:
        session.stderr.append(line)
        session.user_lines.append(line)
        on_stderr(line)

    transport = HostQuitTransport(command, env=env, cwd=cwd, on_stderr=remember)
    # The initialize handshake, as a host sends it. FastMCP 4's default probes
    # server/discover first and settles on the 2026-07-28 era with a FastMCP 4
    # server, a different path from the one every row so far measured.
    client = Client(transport, init_timeout=_INIT_SECONDS, mode="legacy")
    try:
        async with client:
            if started is not None and transport.pid is not None:
                started(transport.pid)
            result = await client.call_tool_mcp(
                tool,
                READ_TOOL_ARGUMENTS if arguments is None else arguments,
                timeout=_CALL_SECONDS,
            )
            session.tool = tool_summary(result)
            session.user_lines += session.tool["text"].splitlines()
            if after_call is not None:
                await after_call()
            if second_call:
                try:
                    again = await client.call_tool_mcp(
                        tool,
                        READ_TOOL_ARGUMENTS if arguments is None else arguments,
                        timeout=_CALL_SECONDS,
                    )
                    session.second_tool = tool_summary(again)
                    session.user_lines += session.second_tool["text"].splitlines()
                except Exception as exc:  # noqa: BLE001 - the recovery's evidence
                    session.second_error = f"{type(exc).__name__}: {exc}"
            await transport.host_quit()
    except Exception as exc:  # noqa: BLE001 - reported as the row's evidence
        detail = f"{type(exc).__name__}: {exc}"
        if transport.quit_done:
            session.teardown_error = detail
        else:
            session.error = detail
    session.pid = transport.pid
    session.alive_before_quit = transport.alive_before_quit
    session.stdin_closed = transport.stdin_closed
    session.stdin_close_error = transport.stdin_close_error
    session.exited_on_quit = transport.exited_on_quit
    session.quit_seconds = transport.quit_seconds
    session.stderr_closed = transport.stderr_closed
    session.killed_by_harness = transport.killed_by_harness
    if transport.process is not None:
        session.exit_code = transport.process.returncode
    return session


def host_failures(session: HostSession) -> list[str]:
    """Why this was not a normal host quit, or nothing when it was.

    A normal quit is a live server whose stdin was closed and which then exited
    by itself with status 0. A crash or a kill may be a row of its own; it is
    never evidence of this one.
    """
    if session.error is not None:
        return [f"the host session failed: {session.error}"]
    failures = []
    if session.alive_before_quit is not True:
        failures.append("the server was already gone before the host quit")
    if session.stdin_closed is not True:
        failures.append(
            f"closing the server's stdin failed: {session.stdin_close_error}"
        )
    if session.killed_by_harness:
        failures.append("the harness had to kill the server")
    if session.exited_on_quit is not True:
        failures.append(
            f"the server did not exit within {_HOST_EXIT_SECONDS}s of stdin EOF"
        )
    elif session.exit_code != 0:
        failures.append(
            f"the server exited abnormally, with status {session.exit_code}, "
            f"after stdin EOF"
        )
    return failures


# --- Process helpers -----------------------------------------------------------


@dataclass
class ProfileCensus:
    """The processes running a browser on the row's profile, and what is unknown.

    ``complete`` only when every process could be judged: its command line
    read, or it established as unrelated the way the watcher establishes it
    (another user, or a readable executable that cannot be the browser), or,
    for a zombie, the whole process shown to have exited. An empty
    ``processes`` from an incomplete census is not an empty profile.
    """

    processes: list[Any] = field(default_factory=list)
    #: Processes whose arguments could not be read and that were not excluded.
    unresolved: list[int] = field(default_factory=list)

    @property
    def pids(self) -> list[int]:
        return [process.pid for process in self.processes]

    @property
    def complete(self) -> bool:
        return not self.unresolved


def thread_count(process: Any) -> int | None:
    """How many threads a process has, or None if that cannot be read."""
    try:
        return process.num_threads()
    except (psutil.Error, OSError):
        pass
    try:
        return len(os.listdir(f"/proc/{process.pid}/task"))
    except OSError:
        return None


def exited_zombie(
    process: Any,
    *,
    linux: bool | None = None,
    threads_of: Callable[[Any], int | None] = thread_count,
) -> bool:
    """Whether a process reported as a zombie has exited as a whole.

    On Linux the status is the thread-group leader's. A leader that ended with
    ``pthread_exit`` is a zombie while the process's other threads run on and
    keep every descriptor and lock it holds, so only a thread count of one,
    the dead leader alone, shows the process gone; an unreadable count shows
    nothing. On macOS a process becomes a zombie only once its last thread has
    exited, and psutil never reports the status on Windows, so there the
    status is enough.
    """
    if not (sys.platform.startswith("linux") if linux is None else linux):
        return True
    return threads_of(process) == 1


def profile_census(
    account: ActorAccount,
    *,
    browser_exe: str | None = None,
    browser_dir: str | Path | None = None,
    process_iter: Callable[..., Iterable[Any]] | None = None,
    user: object | None = None,
) -> ProfileCensus:
    """Every process, root or child, running a browser on this row's profile.

    ``process_iter`` reports a refused read as ``None`` in ``info``, which is
    kept apart from a process that has no such argument.
    """
    owner = harness_user() if user is None else user
    directory = str(browser_dir) if browser_dir is not None else None
    census = ProfileCensus()
    try:
        processes = list(
            (process_iter or psutil.process_iter)(["cmdline", "exe", "status"])
        )
    except psutil.Error:
        return ProfileCensus(unresolved=[-1])
    for process in processes:
        info = getattr(process, "info", {}) or {}
        cmdline = info.get("cmdline")
        if info.get("status") == psutil.STATUS_ZOMBIE:
            # Exited and not yet reaped, then nothing is running there; but
            # a zombie leader can still have threads that hold the profile.
            if exited_zombie(process) or another_user(process_user(process), owner):
                continue
            census.unresolved.append(process.pid)
            continue
        if cmdline is None:
            if another_user(process_user(process), owner):
                continue
            exe = info.get("exe")
            if exe and not possible_browser(exe, browser_exe, directory):
                continue
            census.unresolved.append(process.pid)
            continue
        for argument in cmdline:
            if not argument.startswith(USER_DATA_DIR_FLAG):
                continue
            value = argument[len(USER_DATA_DIR_FLAG) :]
            if canonical_user_data_dir(value) == account.browser_key:
                census.processes.append(process)
            break
    return census


def _profile_processes(account: ActorAccount) -> list[Any]:
    return profile_census(account).processes


def wait_for_no_browser(account: ActorAccount, seconds: float) -> list[int]:
    """Wait for the profile's browser to be gone; the pids still there if not."""
    deadline = time.monotonic() + seconds
    while True:
        remaining = [process.pid for process in _profile_processes(account)]
        if not remaining or time.monotonic() >= deadline:
            return remaining
        time.sleep(0.2)


def sweep_browsers(account: ActorAccount) -> list[int]:
    """Cleanup only: kill whatever still runs on this row's temporary profile.

    Found by the row's own temporary profile in the command line, and killed
    through the handle that lookup returned, which psutil checks against a
    recycled pid before it signals.
    """
    killed = []
    for process in _profile_processes(account):
        with contextlib.suppress(psutil.Error):
            process.kill()
            killed.append(process.pid)
    return killed


# --- Owner identity and cleanup ----------------------------------------------


@dataclass(frozen=True)
class OwnerIdentity:
    """The owner as this row found it, and the one handle that may signal it."""

    pid: int
    create_time: float
    instance_id: str
    auth_root: str
    process: Any = field(compare=False, repr=False)


@dataclass(frozen=True)
class PublishedOwner:
    """What the descriptor on disk names at cleanup."""

    pid: int
    instance_id: str


#: Gone, confirmed: not running before cleanup, or stopped by it and waited for.
GONE = "gone"
STOPPED = "stopped"
#: Anything cleanup could not confirm. Never read as gone.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class OwnerDisposition:
    #: ``gone``, ``stopped`` or ``unknown``.
    state: str
    #: Cleanup sent the owner a signal.
    signalled: bool
    failures: tuple[str, ...] = ()

    @property
    def gone(self) -> bool:
        return self.state in (GONE, STOPPED)


#: How far apart two readings of one create time may be. Both come from psutil
#: on the same machine; the tolerance only absorbs float formatting.
_START_TOLERANCE_SECONDS = 0.01


def row_owner_starts(observed: Iterable[dict[str, Any]]) -> list[tuple[int, float]]:
    """The owners the watcher saw start as this row's actors.

    A ``process.start`` or ``process.update`` it wrote with actor ``owner`` and
    ``in_row`` set: a process that appeared after its baseline, whose ancestry
    at first sight led to this row's harness (the frontend spawns the owner),
    and whose command line was the owner's.
    """
    return [
        (record["pid"], record["start_identity"])
        for record in observed
        if record.get("kind") in ("process.start", "process.update")
        and record.get("actor") == "owner"
        and record.get("in_row") is True
        and isinstance(record.get("pid"), int)
        and isinstance(record.get("start_identity"), (int, float))
    ]


def identify_owner(
    published: Any,
    account: ActorAccount,
    observed: Iterable[dict[str, Any]],
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> tuple[OwnerIdentity | None, str | None]:
    """Bind the descriptor's owner to this row, or say why it cannot be.

    The descriptor says which pid and profile; it cannot say that the process
    now at that pid is the one this row started. The watcher can: it saw the
    row's own frontend start an owner, with a pid and a create time. Only a
    process whose pid and create time match that observation is this row's
    owner, which is what refuses a stale descriptor whose pid another owner
    has since taken.
    """
    try:
        if canonical_user_data_dir(published.profile_path) != account.browser_key:
            return None, "the descriptor serves another profile"
    except (AttributeError, TypeError):
        return None, "the descriptor names no profile"
    try:
        process = open_process(published.pid)
        created = process.create_time()
    except psutil.Error as exc:
        return None, f"pid {published.pid} could not be read ({type(exc).__name__})"
    starts = row_owner_starts(observed)
    if not any(
        pid == published.pid and abs(start - created) <= _START_TOLERANCE_SECONDS
        for pid, start in starts
    ):
        return None, (
            f"pid {published.pid}, created {created}, is not an owner the watcher "
            f"saw this row start (saw {starts})"
        )
    return (
        OwnerIdentity(
            pid=published.pid,
            create_time=created,
            instance_id=published.instance_id,
            auth_root=str(account.auth_root),
            process=process,
        ),
        None,
    )


def settle_owner(
    owner: OwnerIdentity | None,
    published: PublishedOwner | None,
    read_error: str | None,
    *,
    auth_root: str,
    wait_seconds: float = _OWNER_KILL_WAIT_SECONDS,
) -> OwnerDisposition:
    """Decide whether the row's owner is gone, signalling only that owner.

    Three answers: gone (confirmed), stopped (confirmed same-row live, killed
    and waited for), or unknown. Every psutil failure along the way is unknown,
    never gone. Nothing is ever looked up by pid here: the only process that may
    receive a signal is ``owner.process``, the handle taken when the row
    identified it, and the descriptor must still name that pid and instance.
    """

    def unknown(reason: str, *, signalled: bool = False) -> OwnerDisposition:
        return OwnerDisposition(UNKNOWN, signalled, (reason,))

    if read_error is not None:
        return unknown(f"the row's descriptor could not be read: {read_error}")
    if owner is None:
        if published is None:
            return OwnerDisposition(GONE, False)
        return unknown(
            f"the descriptor names pid {published.pid}, instance "
            f"{published.instance_id}, which this row never identified; "
            f"not signalled"
        )
    if owner.auth_root != auth_root:
        return unknown(
            f"the identified owner belongs to {owner.auth_root}; not signalled"
        )
    if published is not None and (
        published.pid != owner.pid or published.instance_id != owner.instance_id
    ):
        return unknown(
            f"the descriptor names pid {published.pid}, instance "
            f"{published.instance_id}; the row identified pid {owner.pid}, "
            f"instance {owner.instance_id}; not signalled"
        )
    try:
        running = owner.process.is_running()
    except psutil.Error as exc:
        return unknown(f"the owner's liveness could not be read ({type(exc).__name__})")
    if not running:
        return OwnerDisposition(GONE, False)
    try:
        owner.process.kill()
    except psutil.NoSuchProcess:
        return OwnerDisposition(GONE, False)
    except psutil.Error as exc:
        return unknown(f"the owner could not be stopped ({type(exc).__name__})")
    try:
        owner.process.wait(timeout=wait_seconds)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        return unknown(
            f"the owner was still running {wait_seconds}s after it was killed",
            signalled=True,
        )
    except psutil.Error as exc:
        return unknown(
            f"the owner's exit could not be confirmed ({type(exc).__name__})",
            signalled=True,
        )
    return OwnerDisposition(STOPPED, True)


@dataclass(frozen=True)
class DaemonCleanup:
    directory: str
    existed: bool
    signalled: bool
    owner_gone: bool
    cleaned: bool
    failures: tuple[str, ...] = ()


def retire_daemon_state(
    account: ActorAccount, owner: OwnerIdentity | None
) -> DaemonCleanup:
    """Settle the row's owner, then remove the row's daemon directory.

    Removed only once the owner is provably gone or was never published.
    Otherwise the directory stays, as the evidence of what was left running.
    """
    directory = daemon_descriptor.daemon_dir(account.auth_root)
    existed = directory.exists()
    published: PublishedOwner | None = None
    read_error: str | None = None
    if existed:
        try:
            descriptor = daemon_descriptor.read(account.auth_root)
        except Exception as exc:  # noqa: BLE001 - the cleanup reports it
            read_error = f"{type(exc).__name__}: {exc}"
        else:
            if descriptor is not None:
                published = PublishedOwner(descriptor.pid, descriptor.instance_id)
    disposition = settle_owner(
        owner, published, read_error, auth_root=str(account.auth_root)
    )
    failures = list(disposition.failures)
    if disposition.gone and existed:
        shutil.rmtree(directory, ignore_errors=True)
        if directory.exists():
            failures.append(f"the row's daemon directory survived removal: {directory}")
    elif existed:
        failures.append(f"the row's daemon directory is kept: {directory}")
    return DaemonCleanup(
        directory=str(directory),
        existed=existed,
        signalled=disposition.signalled,
        owner_gone=disposition.gone,
        cleaned=not directory.exists(),
        failures=tuple(failures),
    )


# --- The outcome of a row ------------------------------------------------------


@dataclass(frozen=True)
class RowVector:
    """What K0 compares across repeats. O1 and O4 are what K3 compares to K1.

    No pid, instance or time: those differ between two healthy runs.
    """

    mode: str
    #: O1: at no sample more than one browser root on the row's profile, from
    #: a watcher whose observation is healthy.
    o1_single_browser: bool
    #: The watcher's positive control: it saw the browser at all.
    browser_seen: bool
    watcher_healthy: bool
    #: O4: the R17 outcome.
    o4_session: str
    origin_saw_feed: bool
    #: A ``/feed/`` request in the row carried the staged session, by the
    #: origin's own judgement.
    feed_carried_session: bool
    tool_succeeded: bool
    #: Daemon mode: a descriptor named the owner. Direct: any sign of one.
    owner_published: bool
    #: Daemon mode was asked for and the frontend drove its own browser.
    fell_back: bool
    host_exit_clean: bool
    #: Cleanup signalled and killed nothing and removed the row's state.
    cleanup_clean: bool
    #: The watcher saw a row actor start the owner module, published or not.
    owner_launched: bool = False
    #: The watcher saw a row actor start the owner's release gate, which is an
    #: attempt to start one whether or not the gate ever released it.
    owner_start_attempted: bool = False
    #: O2 for this row (``signals.derive_o2``): held, violated, unknown, or
    #: unobserved where no signal oracle ran.
    o2: str = O2_UNOBSERVED
    #: Every class of signal the oracle saw delivered, ``role:target kind``.
    signal_classes: tuple[str, ...] = ()
    #: H-R6: the group the killed actor's guardian was told to kill, from its
    #: argv; None where no guardian was seen (Windows has none).
    guardian_owner_group: int | None = None
    #: H-R6, daemon mode: the frontend's second call after the owner was
    #: killed read the post again.
    recovered: bool | None = None


_ASSOCIATE_SECONDS = 5.0


def associate_server(
    pid: int,
    observed: Callable[[], Iterable[dict[str, Any]]],
    *,
    open_process: Callable[[int], Any] = psutil.Process,
    seconds: float = _ASSOCIATE_SECONDS,
) -> tuple[Any, float | None]:
    """The handle to the row's Direct server, once the watcher vouches for it.

    The process at *pid* is the server only if its create time is the one the
    watcher recorded for the row's frontend at that pid; the handle taken for
    that check is the only one the harness will ever kill it through.
    """
    try:
        process = open_process(pid)
        created = process.create_time()
    except psutil.Error:
        return None, None
    deadline = time.monotonic() + seconds
    while True:
        for entry in observed():
            if (
                entry.get("kind") in ("process.start", "process.update")
                and entry.get("actor") == "frontend"
                and entry.get("in_row") is True
                and entry.get("pid") == pid
                and isinstance(entry.get("start_identity"), (int, float))
                and abs(entry["start_identity"] - created) <= _START_TOLERANCE_SECONDS
            ):
                return process, created
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(0.05)


def wait_for_guardian(
    observed: Callable[[], Iterable[dict[str, Any]]],
    principal_pid: int,
    *,
    seconds: float = _ASSOCIATE_SECONDS,
) -> tuple[int, int] | None:
    """The guardian *principal_pid* started, once the watcher has reported it.

    POSIX only: ``start_browser_guardian`` starts none on Windows.
    """
    if os.name == "nt":
        return None
    deadline = time.monotonic() + seconds
    while True:
        found = guardian_launch(observed(), principal_pid)
        if found is not None or time.monotonic() >= deadline:
            return found
        time.sleep(0.05)


def guardian_launch(
    observed: Iterable[dict[str, Any]], principal_pid: int
) -> tuple[int, int] | None:
    """The guardian *principal_pid* started, and its owner-group argument.

    Read from the watcher's records of a row actor started by that process:
    ``<python> -I -S -u .../process_guardian.py <control> <ready> <group>``, as
    ``process_tree.start_browser_guardian`` builds it. None if none was seen.
    """
    found = None
    for entry in observed:
        if entry.get("kind") not in ("process.start", "process.update"):
            continue
        if entry.get("in_row") is not True or entry.get("ppid") != principal_pid:
            continue
        cmdline = entry.get("cmdline")
        if not isinstance(cmdline, list):
            continue
        for index, argument in enumerate(cmdline):
            if Path(str(argument)).name != "process_guardian.py":
                continue
            try:
                found = (int(entry["pid"]), int(cmdline[index + 3]))
            except (IndexError, KeyError, TypeError, ValueError):
                continue
    return found


def owner_launches(observed: Iterable[dict[str, Any]]) -> list[int]:
    """The owner processes the watcher saw this row start.

    A ``process.start`` or ``process.update`` of a row actor (``in_row``: its
    ancestry at first sight led to the harness) whose command line runs the
    owner module with ``-m``. Whether it ever published is not asked: an owner
    that started and gave up is still a coordination effect.
    """
    return sorted(
        {
            record["pid"]
            for record in observed
            if record.get("kind") in ("process.start", "process.update")
            and record.get("in_row") is True
            and isinstance(record.get("cmdline"), list)
            and invoked_module(record["cmdline"]) == OWNER_MODULE
        }
    )


#: What ``process_tree.windows_gate_command`` puts before the gate script, and
#: the separator ``process_gate._arguments`` requires after the nonce.
_GATE_FLAGS = ["-I", "-S", "-u"]
_GATE_NONCE_LENGTH = 64
_HEX = frozenset("0123456789abcdefABCDEF")


def gate_script(checkout: Path) -> Path:
    """The release gate a runtime's own ``windows_gate_command`` names."""
    return checkout / "linkedin_mcp_server" / "process_gate.py"


def _same_file(path: str, expected: Path) -> bool:
    spelled = os.path.normcase(os.path.realpath(path))
    return spelled == os.path.normcase(os.path.realpath(expected))


def owner_gate(cmdline: Sequence[str], gates: Sequence[Path]) -> bool:
    """Whether a command line is the product's owner release gate.

    Exactly what ``process_tree.windows_gate_command`` builds around the
    owner: ``<python> -I -S -u <runtime>/linkedin_mcp_server/process_gate.py
    <nonce> -- <python> ... -m linkedin_mcp_server.daemon_owner ...``, with
    the gate one of *gates* after resolution, the nonce the 64 hex digits
    ``process_gate`` accepts, and the target running the owner module by the
    interpreter's own option grammar. The gate holds the owner until the
    frontend releases it, so it shows an owner start was attempted, not that
    owner code ran.
    """
    command = list(cmdline)
    if len(command) < 8 or command[1:4] != _GATE_FLAGS:
        return False
    if not any(_same_file(command[4], gate) for gate in gates):
        return False
    nonce = command[5]
    if len(nonce) != _GATE_NONCE_LENGTH or not set(nonce) <= _HEX:
        return False
    if command[6] != "--":
        return False
    return invoked_module(command[7:]) == OWNER_MODULE


def owner_gates(observed: Iterable[dict[str, Any]], gates: Sequence[Path]) -> list[int]:
    """The owner release gates the watcher saw this row start.

    Row actors only, as for ``owner_launches``, and never asked for their
    environment.
    """
    return sorted(
        {
            record["pid"]
            for record in observed
            if record.get("kind") in ("process.start", "process.update")
            and record.get("in_row") is True
            and isinstance(record.get("cmdline"), list)
            and owner_gate(record["cmdline"], gates)
        }
    )


def row_expectations(
    vector: RowVector, *, expect_owner: bool | None = None
) -> list[str]:
    """What a row requires; each unmet one is a failure.

    *expect_owner* says whether the row should reach a shared owner, which is
    the configured mode unless the row says otherwise: H-R12 enables the
    daemon with a custom browser and requires the Direct behaviour.
    """
    if expect_owner is None:
        expect_owner = vector.mode == "daemon"
    failures = []
    if not vector.watcher_healthy:
        failures.append("the watcher's observation cannot carry O1")
    if not vector.browser_seen:
        failures.append("the watcher never saw a browser on the row's profile")
    if not vector.o1_single_browser:
        failures.append("O1: a second browser ran on the profile, or O1 is unknown")
    if not vector.origin_saw_feed:
        failures.append("the synthetic origin logged no /feed/ request in the row")
    if not vector.feed_carried_session:
        failures.append("no /feed/ request in the row carried the staged session")
    if not vector.tool_succeeded:
        failures.append(f"{READ_TOOL} did not return the synthetic post")
    if vector.o4_session != RETAINED:
        failures.append(f"O4: the session was {vector.o4_session}, not retained")
    if not vector.host_exit_clean:
        failures.append("the host quit was not a normal one")
    if not vector.cleanup_clean:
        failures.append("cleanup had to intervene or could not finish")
    if vector.o2 == O2_VIOLATED:
        failures.append("O2: a signal reached a wrong target, or a canary died")
    elif vector.o2 == O2_UNKNOWN:
        failures.append("O2: a traced signal's sender or target could not be placed")
    if expect_owner:
        if not vector.owner_published:
            failures.append("daemon mode published no owner")
        if vector.fell_back:
            failures.append("daemon mode fell back to a Direct server")
    else:
        if vector.owner_published:
            failures.append("a row that must stay Direct reached a shared owner")
        if vector.owner_launched:
            failures.append(
                "a row that must stay Direct started a shared owner process "
                "(the watcher saw it; publication is not required)"
            )
        if vector.owner_start_attempted:
            failures.append(
                "a row that must stay Direct started the shared owner's release "
                "gate (an attempted owner start, released or not)"
            )
    return failures


def coordination_reading(vector: RowVector) -> str:
    """H-R12's reading: ``!`` when a shared owner took part, ``=`` when none did.

    An owner published (a descriptor named one, or in a Direct-configured row
    any sign of one), an owner process or owner release gate the row started,
    or the frontend forwarding to one. With the daemon enabled, ``fell_back``
    false means the frontend forwarded.
    """
    forwarded = vector.mode == "daemon" and not vector.fell_back
    coordinated = (
        vector.owner_published
        or vector.owner_launched
        or vector.owner_start_attempted
        or forwarded
    )
    return "!" if coordinated else "="


def k2_r12_verdict(result: RowResult) -> list[str]:
    """K2 on H-R12: the baseline must be caught coordinating despite the setting.

    The baseline elects and uses an owner with a custom browser configured
    (W-CHROME-PATH). A K2 row reading ``=`` there is the harness failing to see
    a known ``!``, so it stops the stage; the rest of K2's outcome is evidence
    of the baseline, not a requirement of it. The reading has to rest on a row
    that ran: a host that never got to call the tool reads nothing, and a
    baseline row that ran candidate code measured the wrong thing.
    """
    problems = list(result.runtime_failures)
    host = result.host
    if result.vector is None or host is None:
        return [*problems, "K2 produced no vector to read"]
    if host.error is not None:
        problems.append(f"K2 could not be read: the host session failed: {host.error}")
    elif coordination_reading(result.vector) != "!":
        problems.append(
            "K2 read '=' on H-R12, where the baseline's '!' is known (it elects "
            "a shared owner despite CHROME_PATH): a harness defect"
        )
    if result.cleanup is not None and result.cleanup.failures:
        problems.append(
            f"cleanup could not settle the baseline's owner: "
            f"{list(result.cleanup.failures)}"
        )
    return problems


#: The class of the pre-Path-A guardian's ``killpg(owner_group)``.
GUARDIAN_OWNER_GROUP_KILL = "guardian:principal-group"


def r6_reading(result: RowResult) -> str | None:
    """H-R6's O2 reading for the owner's guardian: ``!``, ``=``, or None.

    ``!`` when the killed owner's guardian was given a nonzero group to kill
    (its argv, read by the watcher on every POSIX platform) or was seen by the
    oracle killing its principal's group (Linux). The argv is what the reading
    rests on: it is fixed before the kill, needs no tracing, and is the change
    Path A makes; the oracle, where it runs, must agree with it. None where no
    guardian exists (Windows) or none was seen.
    """
    vector = result.vector
    if vector is None or vector.guardian_owner_group is None:
        return None
    if vector.guardian_owner_group != 0:
        return "!"
    if GUARDIAN_OWNER_GROUP_KILL in vector.signal_classes:
        return "!"
    return "="


def r6_verdict(result: RowResult, *, experiment: str, windows: bool) -> list[str]:
    """What H-R6 requires of an experiment beyond the row's own expectations.

    K2 (baseline daemon) must read ``!``: before Path A its owner's guardian
    kills the owner's group, which no Direct guardian does. Reading ``=`` there
    is the harness missing a known difference. K3 (candidate daemon) must read
    ``=`` and have recovered on the second call. K1 (Direct killed) reads ``=``
    as the non-leader server a host starts. On Windows there is no guardian, so
    the reading is not applicable and nothing is required of it.
    """
    problems = list(result.runtime_failures)
    killed = result.killed or {}
    if killed.get("exit") != "killed":
        problems.append(f"the harness did not kill the actor: {killed}")
    if windows:
        return problems
    reading = r6_reading(result)
    vector = result.vector
    if reading is None:
        return [*problems, "the killed actor's guardian was never seen"]
    assert vector is not None
    oracle = killed.get("oracle") or {}
    group_kill_seen = GUARDIAN_OWNER_GROUP_KILL in vector.signal_classes
    if experiment == "K2":
        if reading != "!":
            problems.append(
                "K2 read '=' on H-R6, where the baseline's '!' is known (its "
                "owner's guardian gets the owner's group): a harness defect"
            )
        elif oracle.get("attached") and not group_kill_seen:
            problems.append(
                "K2's guardian had a group to kill, but the signal oracle saw no "
                "kill of the owner's group: a harness defect"
            )
        return problems
    if reading != "=":
        problems.append(
            f"{experiment} read '!' on H-R6: the killed actor's guardian was told "
            f"to kill group {vector.guardian_owner_group}, or the oracle saw it do so"
        )
    if experiment == "K3" and vector.recovered is not True:
        problems.append("the frontend did not recover on the call after the kill")
    return problems


def compare_repeat(first: RowVector, second: RowVector) -> list[str]:
    """K0: every field of two runs of the same experiment must agree."""
    one, two = asdict(first), asdict(second)
    return [
        f"{name}: {one[name]!r} then {two[name]!r}"
        for name in one
        if one[name] != two[name]
    ]


def compare_to_direct(direct: RowVector, daemon: RowVector) -> list[str]:
    """K3 against a Direct reference: O1, O2 and O4 must be ``=``.

    O2 is ``=`` when the per-row state is the same and the daemon row sent no
    class of signal that neither Direct's construction nor the reference's run
    sends. Two rows without an oracle compare as equal and unobserved.
    """
    differences = []
    for name in ("o1_single_browser", "o2", "o4_session"):
        if getattr(direct, name) != getattr(daemon, name):
            differences.append(
                f"{name}: Direct {getattr(direct, name)!r}, daemon "
                f"{getattr(daemon, name)!r}"
            )
    extra = classes_direct_would_not_send(daemon.signal_classes, direct.signal_classes)
    if extra:
        differences.append(f"o2: signals Direct would not send: {extra}")
    return differences


def feed_requests(requests: Sequence[Any]) -> list[Any]:
    return [
        request
        for request in requests
        if request.path.split("?", 1)[0] == "/feed/"
        and (request.host or "").split(":", 1)[0] == "www.linkedin.com"
    ]


@dataclass
class PostQuit:
    """One short Direct session after the row, on the same profile."""

    #: The origin accepted the staged session; None if it could not be asked.
    valid: bool | None
    failures: list[str] = field(default_factory=list)
    user_lines: list[str] = field(default_factory=list)
    feed_requests: int = 0


@dataclass
class Observations:
    """Everything a row observed, for ``judge_row`` to decide on."""

    daemon: bool
    browser_key: str
    host: HostSession
    owner: dict[str, Any]
    cleanup: DaemonCleanup
    swept: list[int]
    residual: list[int]
    watcher: dict[str, Any] | None
    actors_began: float
    actors_ended: float
    row_requests: list[Any]
    before: ProfileSnapshot
    after: ProfileSnapshot | None
    post_quit: PostQuit | None
    #: Whether the row should reach a shared owner; the mode's when None.
    expect_owner: bool | None = None
    #: Daemon state was present for the row's auth root at cleanup.
    daemon_state_existed: bool = False
    #: Owner processes the watcher saw a row actor start (``owner_launches``).
    owner_launches: list[int] = field(default_factory=list)
    #: Owner release gates the watcher saw a row actor start (``owner_gates``).
    owner_gates: list[int] = field(default_factory=list)
    #: O2 as ``signals.derive_o2`` found it; None reads as unobserved.
    o2: O2Result | None = None
    #: H-R6: what the harness killed, and the guardian it had.
    killed: dict[str, Any] | None = None


@dataclass
class RowResult:
    experiment: str
    mode: str
    #: The column this result stands in, when not the mode's default.
    reference: str | None = None
    vector: RowVector | None = None
    before: ProfileSnapshot | None = None
    after: ProfileSnapshot | None = None
    host: HostSession | None = None
    owner: dict[str, Any] | None = None
    watcher: dict[str, Any] | None = None
    cleanup: DaemonCleanup | None = None
    post_quit: PostQuit | None = None
    failures: list[str] = field(default_factory=list)
    #: A frozen row whose actors could not be shown to run the baseline.
    runtime_failures: list[str] = field(default_factory=list)
    #: H-R6: what the harness killed, its guardian and the oracle's state.
    killed: dict[str, Any] | None = None
    o2: O2Result | None = None

    @property
    def label(self) -> str:
        default = DIRECT_REFERENCE if self.mode == "direct" else self.mode
        return f"{self.experiment} ({self.reference or default})"

    def report(self) -> str:
        lines = [f"{self.label} failures:"]
        lines += [f"  - {failure}" for failure in self.failures]
        lines.append(f"vector: {self.vector}")
        if self.host is not None:
            lines.append(f"tool: {self.host.tool}")
            lines.append(f"host error: {self.host.error}")
            lines.append("stderr tail:")
            lines += [f"  {line}" for line in self.host.stderr[-40:]]
        if self.owner is not None:
            lines.append(f"owner: {self.owner.get('pid')} {self.owner.get('exit')}")
            lines += [f"  {line}" for line in self.owner.get("log_tail", [])[-40:]]
        lines.append(f"watcher: {self.watcher}")
        lines.append(f"cleanup: {self.cleanup}")
        return "\n".join(lines)


def judge_row(observed: Observations) -> tuple[RowVector, list[str]]:
    """The row's vector and every failure, from its observations alone."""
    host = observed.host
    failures: list[str] = []
    killed = observed.killed or {}

    if killed.get("actor") == "frontend" and killed.get("exit") == "killed":
        # H-R6 in Direct: the server is the host's own process and the harness
        # killed it after its call, so it cannot quit. Only a failure before
        # that kill counts against the host.
        host_problems = [f"the host session failed: {host.error}"] if host.error else []
    else:
        host_problems = host_failures(host)
    failures += host_problems

    o2 = observed.o2
    if o2 is not None:
        failures += [f"O2 violation: {line}" for line in o2.violations]
        failures += [f"O2 unknown: {line}" for line in o2.unknowns]

    watcher_problems = watcher_failures(
        observed.watcher,
        actors_began=observed.actors_began,
        actors_ended=observed.actors_ended,
    )
    failures += watcher_problems
    summary = observed.watcher or {}
    most_roots = (summary.get("max_roots") or {}).get(observed.browser_key, 0)

    forwarded = any(_FORWARDING_LINE in line for line in host.stderr)
    owner = observed.owner
    expect_owner = (
        observed.daemon if observed.expect_owner is None else observed.expect_owner
    )
    if not expect_owner and observed.daemon and observed.daemon_state_existed:
        # Enabled but ineligible: no coordination effect at all, state included.
        # The harness only ever tests for this directory and never creates it
        # on such a row, so an actor of the row did.
        failures.append(
            f"a row that must stay Direct left daemon state for its auth root: "
            f"{observed.cleanup.directory}"
        )
    if observed.daemon and expect_owner:
        owner_published = bool(owner.get("pid"))
        if owner.get("identify_error"):
            failures.append(
                f"the owner could not be identified: {owner['identify_error']}"
            )
        if owner.get("pid") and (owner.get("exit") or {}).get("how") != "exited":
            failures.append(
                f"the owner did not exit within {_OWNER_EXIT_SLACK_SECONDS}s of "
                f"its {IDLE_TIMEOUT_SECONDS}s idle timeout"
            )
    else:
        owner_published = (
            bool(owner.get("descriptor_present")) or bool(owner.get("pid")) or forwarded
        )

    cleanup = observed.cleanup
    failures += list(cleanup.failures)
    if observed.swept:
        failures.append(f"cleanup had to kill browsers: {observed.swept}")
    if observed.residual:
        failures.append(
            f"a browser on the profile outlived the row by "
            f"{_BROWSER_GONE_SECONDS}s: {observed.residual}"
        )
    cleanup_clean = (
        cleanup.cleaned
        and not cleanup.signalled
        and not cleanup.failures
        and not observed.swept
        and not observed.residual
    )

    post_quit = observed.post_quit
    if post_quit is None:
        failures.append("the post-quit observation did not run")
    else:
        failures += post_quit.failures

    row_feed = feed_requests(observed.row_requests)
    user_lines = list(host.user_lines)
    if post_quit is not None:
        user_lines += post_quit.user_lines
    if observed.after is None:
        o4 = UNCERTAIN
    else:
        o4 = r17_outcome(
            observed.before,
            observed.after,
            user_lines,
            post_quit=post_quit.valid if post_quit is not None else None,
        )

    vector = RowVector(
        mode="daemon" if observed.daemon else "direct",
        o1_single_browser=not watcher_problems and most_roots <= 1,
        browser_seen=most_roots >= 1,
        watcher_healthy=not watcher_problems,
        o4_session=o4,
        origin_saw_feed=bool(row_feed),
        feed_carried_session=any(r.session_valid is True for r in row_feed),
        tool_succeeded=(
            host.tool is not None
            and not host.tool["is_error"]
            and host.tool["read_the_post"]
        ),
        owner_published=owner_published,
        fell_back=observed.daemon and not forwarded,
        owner_launched=bool(observed.owner_launches),
        owner_start_attempted=bool(observed.owner_gates),
        host_exit_clean=not host_problems,
        cleanup_clean=cleanup_clean,
        o2=o2.state if o2 is not None else O2_UNOBSERVED,
        signal_classes=o2.classes if o2 is not None else (),
        guardian_owner_group=killed.get("guardian_owner_group"),
        recovered=_recovered(host) if killed and observed.daemon else None,
    )
    return vector, row_expectations(vector, expect_owner=expect_owner) + failures


def _recovered(host: HostSession) -> bool:
    """The call after the owner was killed read the post again."""
    second = host.second_tool
    return bool(second and not second["is_error"] and second["read_the_post"])


def repeat_verdict(reference: RowVector | None, result: RowResult) -> list[str]:
    """K0: the repeat is valid on its own, and reads as the reference did."""
    problems = []
    if reference is None:
        problems.append("no valid K3 result in this run to repeat")
    if result.failures:
        problems.append(f"the repeat failed its own expectations: {result.failures}")
    if result.vector is None:
        problems.append("the repeat produced no vector")
    elif reference is not None:
        problems += compare_repeat(reference, result.vector)
    return problems


# --- Running the row -----------------------------------------------------------


def preservation_refusals(
    cleanup: DaemonCleanup,
    *,
    owner_exit: str | None,
    residual: Sequence[int],
    swept: Sequence[int],
    remaining: Sequence[int],
    census_unresolved: Sequence[int] = (),
    open_possible_browsers: Sequence[dict[str, Any]] = (),
) -> list[str]:
    """Why the post-quit session must not start, or nothing when it may.

    It may start only when every actor of the row is settled: the owner, if
    there was one, observed to exit and confirmed gone by cleanup; the profile's
    browser census empty and resolved; and cleanup finished without anything
    kept or killed. Launching another server to find out that authority was
    uncertain is exactly what this refuses.

    Settled also means nothing unknown could still be on the profile: a census
    whose arguments could not all be read is not an empty one, and a process
    the watcher still holds as an unresolved possible browser is not gone. A
    finished episode judged by its executable, such as ``/bin/ps``, is neither.
    """
    reasons = []
    if owner_exit not in (None, "exited"):
        reasons.append(f"the owner's exit was {owner_exit!r}")
    if not cleanup.owner_gone:
        reasons.append("cleanup could not confirm the owner gone")
    if not cleanup.cleaned or cleanup.failures:
        reasons.append(f"cleanup did not finish: {list(cleanup.failures)}")
    if residual:
        reasons.append(f"browsers outlived the row: {list(residual)}")
    if swept:
        reasons.append(f"cleanup had to kill browsers: {list(swept)}")
    if remaining:
        reasons.append(f"browsers still run on the profile: {list(remaining)}")
    if census_unresolved:
        reasons.append(
            f"the profile census is incomplete; unreadable: {list(census_unresolved)}"
        )
    if open_possible_browsers:
        reasons.append(
            f"the watcher still holds unresolved possible browsers: "
            f"{[e.get('pid') for e in open_possible_browsers]}"
        )
    return reasons


async def resolved_browser_executable(profile: Path) -> str | None:
    """The executable the product would launch for this row, or None.

    Asked of the product's own launch path with its configured options, so it
    names the same binary the actors will run. None if it cannot say; the
    watcher then still treats anything under the browsers directory as a
    possible browser.
    """
    from patchright.async_api import async_playwright

    from linkedin_mcp_server.browser_launch import build_launch_options
    from linkedin_mcp_server.config import get_config
    from linkedin_mcp_server.core.browser import BrowserManager

    try:
        options, viewport = build_launch_options(get_config().browser)
        probe = BrowserManager(
            user_data_dir=profile, headless=True, viewport=viewport, **options
        )
        playwright = await async_playwright().start()
        try:
            probe._playwright = playwright
            return probe._executable_about_to_run()
        finally:
            probe._playwright = None
            await playwright.stop()
    except Exception:  # noqa: BLE001 - the directory rule still covers it
        return None


async def observe_preservation(
    account: ActorAccount,
    origin: SyntheticOrigin,
    proxy: EgressProxy,
    *,
    command: Sequence[str],
    browsers: Path,
    work_dir: Path,
    on_stderr: Callable[[str], None],
    chrome_path: str | None = None,
) -> PostQuit:
    """Start one Direct host on the profile and ask the origin about its session.

    After the row's interval, with its actors gone, through the same proxy and
    fence. Nothing is re-staged first: that would repair the loss this exists
    to see. Its own browser has to be gone before the row is judged.
    """
    mark = len(origin.requests)
    session = await run_host_session(
        command,
        env=actor_environment(
            account, proxy.url, daemon=False, browsers=browsers, chrome_path=chrome_path
        ),
        cwd=work_dir,
        on_stderr=on_stderr,
    )
    failures = [f"post-quit: {problem}" for problem in host_failures(session)]
    residual = await asyncio.to_thread(
        wait_for_no_browser, account, _BROWSER_GONE_SECONDS
    )
    if residual:
        failures.append(f"post-quit: its browser outlived it: {residual}")
        swept = sweep_browsers(account)
        if swept:
            failures.append(f"post-quit: cleanup had to kill browsers: {swept}")
    feeds = feed_requests(origin.requests[mark:])
    if any(request.session_valid is True for request in feeds):
        valid: bool | None = True
    elif session.error is not None:
        valid = None
    else:
        valid = False
    return PostQuit(
        valid=valid,
        failures=failures,
        user_lines=list(session.user_lines),
        feed_requests=len(feeds),
    )


async def measure_host_quit_row(
    *,
    profile: Path,
    experiment: str,
    daemon: bool,
    egress: tuple[SyntheticOrigin, EgressProxy],
    log: EventLog,
    work_dir: Path,
    command: Sequence[str] | None = None,
    runtime: Runtime | None = None,
    row: str = ROW_H_R1,
    custom_browser: bool = False,
    expect_owner: bool | None = None,
    reference: str | None = None,
    kill_actor: bool = False,
) -> RowResult:
    """Run a host-quit row once and return its outcome vector and evidence.

    *runtime* is the code the actors run: this checkout by default, or the
    frozen baseline, whose actors, staging and browser are all its own.
    *custom_browser* sets ``CHROME_PATH`` to that runtime's bundled Chromium.
    *kill_actor* makes it H-R6: after the call the harness kills the owner
    (daemon) or the server (Direct), through the handle it took when it tied
    that process to the watcher's record of it, with the signal oracle
    attached to it and its guardian first; in daemon mode the host then calls
    again, which is where the frontend recovers.
    """
    # First, before anything reads, launches or spawns.
    account = claim_account(profile)

    origin, proxy = egress
    mode = "daemon" if daemon else "direct"
    if expect_owner is None:
        expect_owner = daemon
    result = RowResult(experiment=experiment, mode=mode, reference=reference)
    runtime = runtime or candidate_runtime()
    command = list(command or runtime.command())

    def emit(actor: str, kind: str, **fields: Any) -> None:
        log.emit(experiment=experiment, row=row, actor=actor, kind=kind, **fields)

    if runtime.frozen:
        identity = frozen_identity(runtime)
        refusal = frozen_refusal(identity, runtime)
        if refusal is not None:
            raise BaselineRefused(refusal)
    else:
        identity = row_identity()
        refusal = evidence_refusal(identity, ci=bool(os.environ.get("CI")))
        if refusal is not None:
            raise EvidenceRefused(refusal)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    emit("harness", "row.identity", mode=mode, **identity)

    browsers = runtime.browsers
    if runtime.frozen:
        # Written here, validated and committed by the baseline's own code and
        # browser, so the profile never meets a newer Chromium first.
        staged = write_synthetic_cookie_file(portable_cookie_path(account.profile))
        origin.accept_session(staged.li_at)
        await asyncio.to_thread(
            stage_frozen_session,
            runtime,
            account.profile,
            actor_environment(account, proxy.url, daemon=False, browsers=browsers),
        )
    else:
        staged = await stage_signed_in_session(
            account.profile,
            accept=lambda session: origin.accept_session(session.li_at),
        )
    # The staging browser has confirmed its close, but a root still on the
    # profile when the watcher takes its baseline would count against O1.
    lingering = await asyncio.to_thread(
        wait_for_no_browser, account, _BROWSER_GONE_SECONDS
    )
    if lingering:
        raise RuntimeError(f"the staging browser is still running: {lingering}")
    before = snapshot(account.profile, expected_digest=staged.li_at_digest)
    result.before = before
    emit("harness", "profile.snapshot", phase="before", **before.as_event_fields())

    request_mark, decision_mark = len(origin.requests), len(proxy.decisions)
    if runtime.frozen:
        browser_exe: str | None = await asyncio.to_thread(bundled_executable, runtime)
    else:
        browser_exe = await resolved_browser_executable(account.profile)
    chrome_path: str | None = None
    if custom_browser:
        if not browser_exe:
            raise RuntimeError(
                "the runtime's bundled browser could not be named, so CHROME_PATH "
                "cannot point at the same binary"
            )
        chrome_path = browser_exe
    env = actor_environment(
        account, proxy.url, daemon=daemon, browsers=browsers, chrome_path=chrome_path
    )
    emit(
        "harness",
        "row.identity",
        browser_exe=browser_exe,
        browsers=str(browsers),
        chrome_path=chrome_path,
        interpreter=runtime.python,
    )
    watcher = Watcher(
        work_dir,
        log,
        experiment=experiment,
        row=row,
        browser_exe=browser_exe,
        browser_dir=browsers,
    )
    watcher.start()
    # After the watcher's baseline, so it reports the canaries' starts and a
    # signal aimed at one resolves to it.
    canaries = Canaries()
    for canary in canaries.start():
        emit("canary", "canary.start", **canary.as_event_fields())
    canary_problems = canaries.outside_the_harness()
    oracle = SignalOracle(work_dir)
    emit(
        "harness",
        "signal.oracle",
        phase="setup",
        available=oracle.available,
        reason=oracle.unavailable,
        ptrace_scope=oracle.scope,
    )
    actors_began = time.time()

    owner: dict[str, Any] = {}
    identified: OwnerIdentity | None = None
    killed: dict[str, Any] = {}
    server: dict[str, int] = {}

    async def kill_the_actor() -> None:
        """Associate the victim, find its guardian, attach the oracle, kill."""
        if daemon:
            if identified is None:
                killed["exit"] = "not killed: the owner was never identified"
                return
            role, pid = "owner", identified.pid
            victim, start = identified.process, identified.create_time
        else:
            role, pid = "frontend", server.get("pid", -1)
            victim, start = await asyncio.to_thread(
                associate_server, pid, watcher.observed
            )
            if victim is None:
                killed["exit"] = f"not killed: server {pid} was never associated"
                return
        guardian = await asyncio.to_thread(wait_for_guardian, watcher.observed, pid)
        killed.update(
            actor=role,
            pid=pid,
            start_identity=start,
            guardian=guardian[0] if guardian else None,
            guardian_owner_group=guardian[1] if guardian else None,
        )
        if oracle.available:
            pids = [pid] + ([guardian[0]] if guardian else [])
            reason = await asyncio.to_thread(oracle.start, pids)
        else:
            reason = oracle.unavailable
        killed["oracle"] = {
            "attached": oracle.available and reason is None,
            "reason": reason,
            "ptrace_scope": oracle.scope,
        }
        try:
            # SIGKILL on POSIX, TerminateProcess on Windows: psutil's kill().
            victim.kill()
            await asyncio.to_thread(victim.wait, _OWNER_KILL_WAIT_SECONDS)
            killed["exit"] = "killed"
        except psutil.NoSuchProcess:
            killed["exit"] = "gone before the kill"
        except psutil.TimeoutExpired:
            killed["exit"] = "still running after the kill"
        except psutil.Error as exc:
            killed["exit"] = f"not killed ({type(exc).__name__})"
        emit("harness", "actor.killed", **killed)

    async def find_the_owner() -> None:
        nonlocal identified
        # A Direct server publishes nothing; a descriptor here would be one.
        # Only looked at, never read into being: ``daemon_descriptor.read``
        # prepares the daemon directory before it reads, so calling it on a
        # row that must leave no daemon state would create that state itself.
        owner["descriptor_present"] = daemon_descriptor.descriptor_path(
            account.auth_root
        ).exists()
        if not daemon or not owner["descriptor_present"]:
            return
        try:
            published = daemon_descriptor.read(account.auth_root)
        except Exception as exc:  # noqa: BLE001 - the row reports it, the host still quits
            owner["read_error"] = f"{type(exc).__name__}: {exc}"
            return
        if published is None:
            return
        owner.update(
            pid=published.pid,
            instance_id=published.instance_id,
            protocol=published.protocol_version,
            log_path=published.log_path,
        )
        identified, problem = identify_owner(published, account, watcher.observed())
        if identified is None:
            owner["identify_error"] = problem
        else:
            owner["start_identity"] = identified.create_time
        emit("harness", "owner.found", **owner)

    async def after_call() -> None:
        await find_the_owner()
        if kill_actor:
            await kill_the_actor()

    after: ProfileSnapshot | None = None
    actors_ended: float | None = None
    residual: list[int] = []
    calls: list[Any] = []
    try:
        host = await run_host_session(
            command,
            env=env,
            cwd=work_dir,
            on_stderr=lambda line: emit(
                "frontend", "user.output", stream="stderr", line=line
            ),
            after_call=after_call,
            started=lambda pid: server.update(pid=pid),
            second_call=kill_actor and daemon,
        )
        result.host = host
        if kill_actor and daemon:
            # The owner the frontend recovered to is the one that now has to
            # leave through its idle exit and be cleaned up.
            owner.clear()
            identified = None
            await find_the_owner()
        if host.tool is not None:
            emit("host_stub", "tool.result", tool=READ_TOOL, **host.tool)
        emit(
            "host_stub",
            "process.exit",
            pid=host.pid,
            alive_before_quit=host.alive_before_quit,
            stdin_closed=host.stdin_closed,
            exit_code=host.exit_code,
            killed_by_harness=host.killed_by_harness,
            quit_seconds=host.quit_seconds,
        )

        if identified is not None:
            began = time.monotonic()
            exit_record: dict[str, Any] = {}
            owner["exit"] = exit_record
            try:
                await asyncio.to_thread(
                    identified.process.wait,
                    IDLE_TIMEOUT_SECONDS + _OWNER_EXIT_SLACK_SECONDS,
                )
                exit_record["how"] = "exited"
                exit_record["seconds_after_quit"] = round(time.monotonic() - began, 3)
            except psutil.TimeoutExpired:
                exit_record["how"] = "still running"
            except psutil.Error as exc:
                exit_record["how"] = f"unknown ({type(exc).__name__})"
            log_path = Path(owner.get("log_path") or "")
            if log_path.is_file():
                lines = log_path.read_text(errors="replace").splitlines()
                owner["log_tail"] = lines[-200:]
                for line in owner["log_tail"]:
                    emit("owner", "user.output", stream="owner-log", line=line)
            # Evidence of which path it took, not a requirement: the line is an
            # INFO record, and whether the owner's log keeps INFO is a setting.
            exit_record["idle_line_seen"] = any(
                _IDLE_EXIT_LINE in line for line in owner.get("log_tail", [])
            )
            emit("harness", "owner.exit", **exit_record)

        residual = await asyncio.to_thread(
            wait_for_no_browser, account, _BROWSER_GONE_SECONDS
        )
        actors_ended = time.time()
        after = snapshot(account.profile, expected_digest=staged.li_at_digest)
        result.after = after
        emit("harness", "profile.snapshot", phase="after", **after.as_event_fields())
    finally:
        if actors_ended is None:
            actors_ended = time.time()
        # Once its tracees have exited, strace has seen every signal they sent.
        calls = await asyncio.to_thread(oracle.stop)
        # The row's interval ends here: the watcher stops before anything else
        # starts on the profile.
        result.watcher = watcher.stop()
        observed_events = watcher.observed()
        canary_deaths = canaries.deaths()
        canaries.stop()
        o2 = derive_o2(
            calls,
            ProcessHistory(observed_events, outside=[os.getpid()]),
            threads=oracle.threads,
            oracle_available=bool((killed.get("oracle") or {}).get("attached")),
            canary_deaths=canary_deaths,
        )
        for call in calls:
            emit("harness", "signal.sent", **call.as_event_fields())
        for resolved in o2.resolved:
            emit("harness", "signal.sent", resolution=resolved)
        for death in canary_deaths:
            emit("canary", "process.death_unattributed", **death)
        for violation in o2.violations:
            emit("watcher", "process.death_unattributed", wrong_target=violation)
        launched = owner_launches(observed_events)
        # The row's own runtime's gate, and the candidate's: a baseline row
        # reaching the candidate's gate is an owner start attempt all the same.
        gated = owner_gates(
            observed_events,
            [gate_script(runtime.checkout), gate_script(REPO_ROOT)],
        )
        if runtime.frozen:
            result.runtime_failures = interpreter_failures(
                observed_events,
                runtime,
                candidate_prefix=sys.prefix,
                owner_expected=bool(owner.get("pid")),
            )
        row_requests = list(origin.requests[request_mark:])
        row_decisions = list(proxy.decisions[decision_mark:])
        result.cleanup = retire_daemon_state(account, identified)
        swept = sweep_browsers(account)
        for request in row_requests:
            emit(
                "origin",
                "browser.request",
                t=request.t or None,
                host=request.host,
                server_name=request.server_name,
                path=request.path,
                cookie_names=list(request.cookie_names),
                session_valid=request.session_valid,
            )
        for decision in row_decisions:
            emit(
                "proxy",
                "proxy.decision",
                method=decision.method,
                target=decision.target,
                host=decision.host,
                port=decision.port,
                forwarded=decision.forwarded,
            )

    host = result.host
    assert host is not None
    census = profile_census(account, browser_exe=browser_exe, browser_dir=browsers)
    refusals = preservation_refusals(
        result.cleanup,
        owner_exit=(owner.get("exit") or {}).get("how") if daemon else None,
        residual=residual,
        swept=swept,
        remaining=census.pids,
        census_unresolved=census.unresolved,
        open_possible_browsers=[
            episode
            for episode in (result.watcher or {}).get("relevant_read_failures") or []
            if episode.get("resolution") == "open"
        ],
    )
    if expect_owner and identified is None:
        refusals.append("the row's owner was never identified")
    post_quit: PostQuit | None
    if refusals:
        # Nothing is launched. The session's fate after the row is unknown, and
        # the reasons are the row's failures.
        post_quit = PostQuit(
            valid=None, failures=[f"post-quit not run: {r}" for r in refusals]
        )
    else:
        post_quit = await observe_preservation(
            account,
            origin,
            proxy,
            command=command,
            browsers=browsers,
            chrome_path=chrome_path,
            work_dir=work_dir,
            on_stderr=lambda line: emit(
                "frontend", "user.output", stream="stderr", phase="post-quit", line=line
            ),
        )
    emit(
        "harness",
        "tool.result",
        phase="post-quit",
        session_valid=post_quit.valid,
        feed_requests=post_quit.feed_requests,
        failures=post_quit.failures,
    )
    result.post_quit = post_quit
    result.owner = owner or None

    result.vector, result.failures = judge_row(
        Observations(
            daemon=daemon,
            browser_key=account.browser_key,
            host=host,
            owner=owner,
            cleanup=result.cleanup,
            swept=swept,
            residual=residual,
            watcher=result.watcher,
            actors_began=actors_began,
            actors_ended=actors_ended,
            row_requests=row_requests,
            before=before,
            after=after,
            post_quit=post_quit,
            expect_owner=expect_owner,
            daemon_state_existed=result.cleanup.existed,
            owner_launches=launched,
            owner_gates=gated,
            o2=o2,
            killed=killed or None,
        )
    )
    result.killed = killed or None
    result.o2 = o2
    result.failures += result.runtime_failures
    result.failures += [f"canary placement: {problem}" for problem in canary_problems]
    (work_dir / "failures.json").write_text(
        json.dumps(
            {
                "label": result.label,
                "row": row,
                "vector": asdict(result.vector),
                "failures": result.failures,
                "coordination": coordination_reading(result.vector),
                "killed": result.killed,
                "o2": asdict(o2),
            },
            indent=2,
        )
        + "\n"
    )
    emit(
        "harness",
        "row.outcome",
        mode=mode,
        reference=reference or (DIRECT_REFERENCE if not daemon else None),
        coordination=coordination_reading(result.vector),
        vector=asdict(result.vector),
        failures=result.failures,
        cleanup=asdict(result.cleanup),
        killed=result.killed,
        o2_state=o2.state,
    )
    return result
