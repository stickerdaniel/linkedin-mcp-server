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
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import asdict, dataclass, field, replace
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
from differential.job_query import (
    Fates,
    PrivateCache,
    ShimVenv,
    StallHost,
    install_locations,
    private_install,
    DrainReading,
    drain_reading,
    reached,
    record_install,
    terminations,
)
from differential.session import (
    RETAINED,
    UNCERTAIN,
    ProfileSnapshot,
    r17_outcome,
    snapshot,
    stage_signed_in_session,
    write_synthetic_cookie_file,
)
from differential.signals import COMPLETE as ORACLE_COMPLETE
from differential.signals import INCOMPLETE as O2_INCOMPLETE
from differential.signals import UNAVAILABLE as ORACLE_UNAVAILABLE
from differential.signals import UNOBSERVED as O2_UNOBSERVED
from differential.signals import VIOLATED as O2_VIOLATED
from differential.signals import (
    ORACLE_REQUIRED,
    Canaries,
    Lifetime,
    O2Result,
    OracleOutcome,
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


def _gap_cause(summary: dict[str, Any]) -> str:
    """Where the largest gap went, so the failure names its cause.

    A gap spent mostly waiting to run between two samples leads with that wait
    and the sample's own share: the slowest sample of the run can be the
    baseline, which no gap is measured across.
    """
    widest: tuple[float, float] | None = None
    log = summary.get("sample_log") or []
    for previous, current in zip(log, log[1:]):
        ended, began, now = previous[1], current[0], current[1]
        if not all(isinstance(t, (int, float)) for t in (ended, began, now)):
            continue
        if widest is None or now - ended > widest[0]:
            widest = (now - ended, began - ended)
    if widest is not None and widest[1] > widest[0] - widest[1]:
        return (
            f"{widest[1]:.4f}s of it passed between two samples, while the "
            f"watcher waited to run (priority {summary.get('priority')!r}), "
            f"and {widest[0] - widest[1]:.4f}s in the sample that closed it"
        )
    slow = sorted(
        summary.get("slow_samples") or [],
        key=lambda entry: entry.get("seconds") or 0,
        reverse=True,
    )
    waited_on = [
        {"sample_seconds": entry.get("seconds"), **(entry.get("slowest") or {})}
        for entry in slow[:3]
    ]
    return f"its slowest samples waited on {waited_on or 'nothing it recorded'}"


def watcher_failures(
    summary: dict[str, Any] | None,
    *,
    actors_began: float,
    actors_ended: float,
    max_gap: float = MAX_WATCHER_GAP_SECONDS,
    browser_key: str | None = None,
) -> list[str]:
    """Why this observation cannot carry O1 for the profile *browser_key*, or
    nothing when it can. Without one, no retained reading is credited."""
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
            f"{max_gap}s this row accepts; {_gap_cause(summary)}"
        )
    # Only an actor that could have been a browser root: one whose executable
    # could not be read, or is the row's browser. Every failed read stays in
    # the summary's ``read_failures`` either way. A known root whose later
    # argument read failed kept its earlier reading, and that reading keeps
    # it counted only on the profile it named: for any other, a same-image
    # exec with hidden arguments could have moved it onto the one judged.
    unread = [
        *(summary.get("relevant_read_failures") or []),
        *(
            entry
            for entry in summary.get("read_failures") or []
            if "retained_profile" in entry and entry["retained_profile"] != browser_key
        ),
    ]
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
    #: What a row's scripted phase called, in order, and how it failed.
    scripted: list[dict[str, Any]] = field(default_factory=list)
    script_error: str | None = None


#: A row's scripted phase: it is handed a function that calls one tool through
#: the host's own client and returns the call's summary.
ToolCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


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
    script: Callable[[ToolCall], Awaitable[None]] | None = None,
) -> HostSession:
    """Initialize, call the read tool once, then quit the way a host does.

    *started* is told the server's pid as soon as it runs. With *second_call*
    the tool is called once more after *after_call*, which is how H-R6 sees the
    frontend recover from a killed owner; its failure is recorded apart and
    does not stop the host from quitting. *script*, when given, runs last
    before the quit with a function that calls tools through this client
    (H-R11); what it called is in ``scripted``, and its failure, recorded as
    ``script_error``, does not stop the quit either.
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
            if script is not None:

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    began = time.time()
                    called = await client.call_tool_mcp(
                        name, arguments, timeout=_CALL_SECONDS
                    )
                    summary: dict[str, Any] = {
                        "tool": name,
                        "began": began,
                        "ended": time.time(),
                        **tool_summary(called),
                    }
                    session.scripted.append(summary)
                    session.user_lines += summary["text"].splitlines()
                    return summary

                try:
                    await script(call)
                except Exception as exc:  # noqa: BLE001 - the script's own evidence
                    session.script_error = f"{type(exc).__name__}: {exc}"
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


#: How long, after the probe, the row waits for the descriptor to name the
#: successor and for the watcher to have seen the browser it launched.
_SUCCESSOR_SECONDS = 10.0


def successor_problems(
    observed: Iterable[Mapping[str, Any]],
    closing: OwnerIdentity | None,
    successor: OwnerIdentity | None,
    *,
    close_began: float,
) -> list[str]:
    """Why *successor* is not shown to have replaced *closing* and served the
    probe; empty when it is.

    A served probe says that some owner answered, not which. The successor
    counts only as a lifetime of its own: another pid and create time and
    another instance than the owner that closed, first seen by the watcher
    after the close began and not yet gone. The probe is the one read after
    the close, so the browser it needed is a launch after the close, and one
    such launch must descend from the successor while none descends from the
    owner that closed. A launch whose ancestry is unknown counts for neither.
    """
    if closing is None:
        return ["the owner that closed was never identified"]
    if successor is None:
        return ["the descriptor names no owner this row started"]
    if (successor.pid, successor.create_time) == (closing.pid, closing.create_time):
        return [f"the descriptor still names the owner that closed, pid {closing.pid}"]
    problems = []
    if successor.instance_id == closing.instance_id:
        problems.append(f"the successor reuses instance {closing.instance_id!r}")
    history = ProcessHistory(observed, outside=[os.getpid()])

    def lifetime(owner: OwnerIdentity) -> Lifetime | None:
        for life in history.lifetimes:
            if (
                life.pid == owner.pid
                and abs(life.start - owner.create_time) <= _START_TOLERANCE_SECONDS
                and life.was("owner")
            ):
                return life
        return None

    old, new = lifetime(closing), lifetime(successor)
    if new is None:
        return [*problems, f"the watcher never saw pid {successor.pid} start"]
    if old is None:
        return [*problems, f"the watcher has no lifetime for pid {closing.pid}"]
    if new.first_t < close_began:
        problems.append(f"pid {successor.pid} was running before the close began")
    if new.exit_t is not None:
        problems.append(f"pid {successor.pid} had exited before the host quit")
    now = time.time()
    launched = [
        life
        for life in history.lifetimes
        if life.in_row and life.was("browser") and life.first_t >= close_began
    ]
    if not any(history.descends(life, new, now) is True for life in launched):
        problems.append(
            f"no browser launched after the close descends from pid {successor.pid}"
        )
    if any(history.descends(life, old, now) is True for life in launched):
        problems.append(
            f"pid {closing.pid}, the owner that closed, launched a browser after "
            f"the close"
        )
    return problems


def settle_owner(
    owner: OwnerIdentity | None,
    published: PublishedOwner | None,
    read_error: str | None,
    *,
    auth_root: str,
    wait_seconds: float = _OWNER_KILL_WAIT_SECONDS,
    linux: bool | None = None,
) -> OwnerDisposition:
    """Decide whether the row's owner is gone, signalling only that owner.

    Three answers: gone (confirmed), stopped (confirmed same-row live, killed
    and waited for), or unknown. Every psutil failure along the way is unknown,
    never gone. Nothing is ever looked up by pid here: the only process that may
    receive a signal is ``owner.process``, the handle taken when the row
    identified it, and the descriptor must still name that pid and instance.
    A zombie is gone only once the whole process has exited (``is_dead``).
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
    try:
        # H-R6 kills the owner, and its parent may not have reaped it yet.
        if not running or is_dead(owner.process, linux=linux):
            return OwnerDisposition(GONE, False)
    except psutil.Error as exc:
        return unknown(f"the owner's liveness could not be read ({type(exc).__name__})")
    try:
        owner.process.kill()
    except psutil.NoSuchProcess:
        return OwnerDisposition(GONE, False)
    except psutil.Error as exc:
        return unknown(f"the owner could not be stopped ({type(exc).__name__})")
    try:
        dead = wait_until_dead(owner.process, wait_seconds, linux=linux)
    except psutil.Error as exc:
        return unknown(
            f"the owner's exit could not be confirmed ({type(exc).__name__})",
            signalled=True,
        )
    if not dead:
        return unknown(
            f"the owner was still running {wait_seconds}s after it was killed",
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
    account: ActorAccount,
    owner: OwnerIdentity | None,
    *,
    linux: bool | None = None,
    wait_seconds: float = _OWNER_KILL_WAIT_SECONDS,
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
        owner,
        published,
        read_error,
        auth_root=str(account.auth_root),
        linux=linux,
        wait_seconds=wait_seconds,
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
    #: O2 for every actor of the row: ``violated`` on evidence (a traced
    #: violation, a dead canary), otherwise ``unobserved``. Never ``held``:
    #: nothing observes the senders outside the traced scope.
    o2: str = O2_UNOBSERVED
    #: O2 for the traced scope only (``signals.derive_o2``): the killed actor
    #: and its guardian from the attach on. ``held``, ``violated``,
    #: ``unknown``, ``incomplete``, or ``unobserved`` where no oracle ran.
    o2_traced: str = O2_UNOBSERVED
    #: The oracle was required here (native Linux CI, a row that kills).
    o2_required: bool = False
    #: The oracle's collection, apart from what it showed: ``complete``,
    #: ``incomplete`` or ``unavailable``. What every required-oracle check reads.
    oracle_collection: str = ORACLE_UNAVAILABLE
    #: Every class of signal the oracle resolved, ``role:target kind``.
    signal_classes: tuple[str, ...] = ()
    #: H-R6: the group the killed actor's guardian was told to kill, from its
    #: argv; None where no guardian was seen (Windows has none).
    guardian_owner_group: int | None = None
    #: H-R6, daemon mode: the frontend's second call after the owner was
    #: killed read the post again.
    recovered: bool | None = None
    #: H-R11: the row-scoped shim's ``IsProcessInJob`` was reached during the
    #: routine drain (K2/K3 daemon reach it; K1 has no adopted Job). None off
    #: the row.
    job_query_reached: bool | None = None
    #: H-R11: a member of another Job the owner holds (the installer) was
    #: terminated with exit code 1 during the routine drain while the owner was
    #: alive. K2 True (``!``); K3 False (``=``). None off the row.
    job_member_terminated: bool | None = None
    #: H-R11 availability: a successor was elected while the host still ran.
    successor_before_quit: bool | None = None


_ASSOCIATE_SECONDS = 5.0


def is_dead(
    process: Any,
    *,
    linux: bool | None = None,
    threads_of: Callable[[Any], int | None] = thread_count,
) -> bool:
    """Whether *process* has ended as a whole, an unreaped one included.

    An owner is not the harness's child, so once killed it stays a zombie
    until its own parent reaps it, and ``psutil`` reads a zombie as running.
    A zombie counts as dead only under ``exited_zombie``'s contract: on Linux
    the status is the leader thread's, and other threads may run on. A read
    that fails for any other reason than the process being gone raises: that
    is not knowing, never dead.
    """
    try:
        zombie = process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True
    return zombie and exited_zombie(process, linux=linux, threads_of=threads_of)


def wait_until_dead(process: Any, seconds: float, *, linux: bool | None = None) -> bool:
    """Whether *process* is dead within *seconds*; ``psutil.Error`` is unknown.

    On Windows, where nothing lingers as a zombie, the handle is waited on.
    """
    if os.name == "nt":
        try:
            process.wait(timeout=seconds)
        except psutil.TimeoutExpired:
            return False
        except psutil.NoSuchProcess:
            pass
        return True
    deadline = time.monotonic() + seconds
    while not is_dead(process, linux=linux):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


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
        failures.append("O2: a signal was aimed at a wrong target, or a canary died")
    # ``unknown`` is the oracle's stated limit (``signals``): a recipient a
    # SIGKILL reached is gone by the next sample. It is recorded, not failed.
    if vector.o2_traced == O2_VIOLATED:
        failures.append("O2: a traced signal reached a process outside its set")
    if vector.o2_required and vector.oracle_collection != ORACLE_COMPLETE:
        failures.append(
            f"O2: the required signal oracle's evidence is "
            f"{vector.oracle_collection}, not complete"
        )
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


def r6_verdict(
    result: RowResult,
    *,
    experiment: str,
    windows: bool,
    linux: bool | None = None,
) -> list[str]:
    """What H-R6 requires of an experiment beyond the row's own expectations.

    K2 (baseline daemon) must read ``!``: before Path A its owner's guardian
    kills the owner's group, which no Direct guardian does. Reading ``=`` there
    is the harness missing a known difference. On Linux the oracle is part of
    that witness: K2 needs a complete required trace that shows the class
    ``guardian:principal-group``, while the rest of K2's outcome stays the
    baseline's to record. On macOS the guardian's argv is the witness. K3
    (candidate daemon) must read ``=`` and have recovered on the second call.
    K1 (Direct killed) reads ``=`` as the non-leader server a host starts. On
    Windows there is no guardian, so the reading is not applicable; the kill
    and K3's recovery still are.
    """
    if linux is None:
        linux = sys.platform.startswith("linux")
    problems = list(result.runtime_failures)
    killed = result.killed or {}
    vector = result.vector
    if killed.get("exit") != "killed":
        problems.append(f"the harness did not kill the actor: {killed}")
    # Every experiment's kill follows a first call that read the synthetic
    # post; one that failed before reading is not the row this measures.
    if vector is None or not vector.tool_succeeded:
        problems.append("the first call did not read the synthetic post")
    if experiment == "K3" and (vector is None or vector.recovered is not True):
        problems.append("the frontend did not recover on the call after the kill")
    if windows:
        return problems
    reading = r6_reading(result)
    if reading is None:
        return [*problems, "the killed actor's guardian was never seen"]
    assert vector is not None
    group_kill_seen = GUARDIAN_OWNER_GROUP_KILL in vector.signal_classes
    traced = vector.o2_required and vector.oracle_collection == ORACLE_COMPLETE
    if linux and not traced:
        problems.append(
            f"the required signal oracle did not deliver a complete trace "
            f"(required={vector.o2_required}, collection "
            f"{vector.oracle_collection!r})"
        )
    if experiment == "K2":
        if reading != "!":
            problems.append(
                "K2 read '=' on H-R6, where the baseline's '!' is known (its "
                "owner's guardian gets the owner's group): a harness defect"
            )
        elif linux and traced and not group_kill_seen:
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
    return problems


_INSTALLER_SECONDS = 60.0
#: How long an owner that stood down after an unconfirmed close may take to go.
_OWNER_STAND_DOWN_SECONDS = 30.0
#: What the product logs when a close could not prove the browser gone
#: (``core/browser.py``, the same at the baseline).
_UNCONFIRMED_CLOSE_LINE = "stays unconfirmed"


def close_left_unconfirmed(log_path: str | None, seconds: float = 2.0) -> bool:
    """Whether the owner's log says its close stayed unconfirmed.

    Read until the line is there or *seconds* pass: ``close_session`` has
    returned, so the drain that decides has logged. False for a log that is
    not there, which leaves the probe where it was.
    """
    if not log_path:
        return False
    path = Path(log_path)
    deadline = time.monotonic() + seconds
    while True:
        if path.is_file() and _UNCONFIRMED_CLOSE_LINE in path.read_text(
            errors="replace"
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def is_installer(record: Mapping[str, Any]) -> bool:
    """A process of the product's installer: supervisor, its gate, the
    ``patchright install`` worker and the Node processes it starts."""
    joined = " ".join(str(part) for part in record.get("cmdline") or [])
    return (
        record.get("actor") == "installer"
        or ("patchright" in joined and " install " in f" {joined} ")
        or "oopBrowserDownload" in joined
    )


def installer_starts(observed: Iterable[Mapping[str, Any]]) -> list[tuple[int, float]]:
    """Every installer lifetime (pid, create time) the watcher recorded in the row."""
    starts: list[tuple[int, float]] = []
    for entry in observed:
        if entry.get("kind") not in ("process.start", "process.update"):
            continue
        if entry.get("in_row") is not True or not is_installer(entry):
            continue
        start = entry.get("start_identity")
        if not isinstance(start, (int, float)):
            continue
        key = (int(entry["pid"]), float(start))
        if key not in starts:
            starts.append(key)
    return starts


def wait_for_installers(
    observed: Callable[[], Iterable[Mapping[str, Any]]],
    *,
    watch: Callable[[int, float], None],
    open_process: Callable[[int], Any] = psutil.Process,
    seconds: float = _INSTALLER_SECONDS,
) -> list[tuple[int, float]]:
    """The row's installer processes, each handed to *watch* as it is found.

    Only when the process at the pid is still the lifetime the watcher
    recorded (its create time), and at once, so the handle *watch* takes names
    that lifetime and keeps its exit readable however it ends. After the
    first is seen, a second look a moment later picks up the worker and the
    download the supervisor starts.
    """
    deadline = time.monotonic() + seconds
    found: list[tuple[int, float]] = []
    settle_until: float | None = None
    while True:
        for key in installer_starts(observed()):
            if key in found:
                continue
            try:
                process = open_process(key[0])
                if abs(process.create_time() - key[1]) > _START_TOLERANCE_SECONDS:
                    continue
            except psutil.Error:
                continue
            found.append(key)
            watch(*key)
            if settle_until is None:
                settle_until = time.monotonic() + 3.0
        now = time.monotonic()
        if (settle_until is not None and now >= settle_until) or now >= deadline:
            return found
        time.sleep(0.1)


def job_query_reading(
    shim: ShimVenv, *, fates: Fates, owner_pid: int | None, daemon: bool
) -> DrainReading:
    """The routine drain's reading from the shim's records of the closing owner.

    In Direct no process has an adopted Job, so every record at all counts.
    """
    pid = owner_pid if daemon else None
    return drain_reading(
        fates.fates.values(),
        reached(shim.reached_file, pid=pid),
        terminations(shim.reached_file, pid=pid),
    )


def successor_verdict(
    *,
    probe: Mapping[str, Any] | None,
    left: bool | None,
    problems: Iterable[str] | None,
) -> list[str]:
    """Why H-R11's recovery is not a successor that served, before host quit.

    All three are needed: the probe read the synthetic post, the owner that
    closed was confirmed gone before it, and the owner the descriptor then
    named is a new lifetime and instance that served it (``successor_problems``
    and a creation time after the close began). A gate, an owner-labelled
    start or a later Direct session stands in for none of them.
    """
    found = []
    if probe is None:
        found.append("no probe was made after the close")
    elif probe.get("is_error") or not probe.get("read_the_post"):
        found.append(
            f"the probe after the close did not read the synthetic post "
            f"(is_error={probe.get('is_error')!r})"
        )
    if left is not True:
        found.append(
            f"the owner that closed was not confirmed gone before the probe "
            f"(left={left!r})"
        )
    if problems is None:
        # Nobody looked for a new owner, so nothing shows there was one.
        return [*found, "the successor was never looked for"]
    return [*found, *problems]


def job_query_observations(
    shim: ShimVenv | None,
    *,
    reading: DrainReading | None,
    window: Mapping[str, Any],
    owner_pid: int | None,
    daemon: bool,
) -> dict[str, Any]:
    """What H-R11 feeds ``judge_row``; nothing off the row.

    Reached: the shim recorded a planted failure in the process whose drain
    it was, the owner serving at the close (none expected in Direct, which has
    no adopted Job, so any line at all counts there). Terminated: the routine
    drain's reading (``drain_reading``), None while any of its evidence is
    unknown. Successor: verified before host quit (``successor_verdict``).
    """
    if shim is None:
        return {}
    lines = reached(shim.reached_file, pid=owner_pid if daemon else None)
    return {
        "failed_job_query": True,
        "job_query_reached": bool(lines),
        "job_member_terminated": reading.value if reading is not None else None,
        "successor_before_quit": (
            window.get("successor_verified") is True if daemon else None
        ),
    }


def job_query_problems(
    shim: ShimVenv | None,
    *,
    fates: Fates,
    reading: DrainReading | None,
    window: Mapping[str, Any],
    script_error: str | None,
) -> list[str]:
    """What stops H-R11 from observing the row, in any experiment.

    Not the behaviour under test: a K2 that reads its known '!' still fails on
    one of these, since the '!' is then not established by complete evidence.
    """
    if shim is None:
        return []
    problems = []
    if script_error is not None:
        problems.append(f"the H-R11 script failed: {script_error}")
    if not fates.fates:
        problems.append(
            "no installer ran when the row closed, so the Job query had no "
            "member to be asked about"
        )
    if reading is None:
        problems.append("the drain's reading was never taken")
    else:
        problems += [f"installer evidence: {unknown}" for unknown in reading.unknown]
    if window.get("script_ended") is not True:
        problems.append("the H-R11 script did not run to its end")
    return problems


def r11_reading(result: RowResult) -> str | None:
    """H-R11's reading for the routine drain after the Job query fails.

    ``!`` when the routine drain terminated a member of another Job the owner
    holds (the installer) after its planted query failed, ``=`` when complete
    evidence shows it terminated none, and None where the evidence is
    incomplete or the row is off this path.
    """
    vector = result.vector
    if vector is None or vector.job_member_terminated is None:
        return None
    return "!" if vector.job_member_terminated else "="


def r11_verdict(
    result: RowResult,
    *,
    experiment: str,
    non_windows: bool,
) -> list[str]:
    """What H-R11 requires of an experiment beyond the row's own expectations.

    Windows only. K1 frozen is the reference: it has no adopted Job, so the
    shimmed query is never reached, and it ends with host quit. K2 (baseline
    daemon) swallows the failed ``IsProcessInJob`` and so terminates a member
    of another owned Job (the installer) with exit code 1 while it is alive, so
    K2 must read ``!`` and must have reached the query. K3 (candidate daemon)
    counts that member and terminates nothing, so it must read ``=``, have
    reached the query, and elected a successor while the host still ran.

    Every experiment also carries the row's observation failures
    (``job_query_problems``): K2 keeps its known behavioural '!', but not with
    evidence the harness could not complete or clean up after.
    """
    problems = [*result.runtime_failures, *result.observation_failures]
    if non_windows:
        return [*problems, "H-R11 runs on Windows only; it should be skipped"]
    vector = result.vector
    if vector is None or not vector.tool_succeeded:
        problems.append("the first call did not read the synthetic post")
    reading = r11_reading(result)
    if experiment == "K1":
        if vector is not None and vector.job_query_reached:
            problems.append(
                "the Direct reference reached the Job-membership query, but it has "
                "no adopted Job to reach it through"
            )
        return problems
    if vector is not None and not vector.job_query_reached:
        problems.append("the failing Job-membership query was never reached")
    if experiment == "K2":
        if reading != "!":
            problems.append(
                f"K2 read {reading!r} on H-R11, where the baseline's '!' is known "
                f"(it swallows the failed query and terminates the member): a "
                f"harness defect"
            )
        return problems
    # K3
    if reading == "!":
        problems.append(
            "K3 read '!' on H-R11: the candidate terminated a member of another "
            "owned Job on a failed query"
        )
    elif reading is None:
        problems.append("K3 has no complete reading of the routine drain")
    if vector is not None and vector.successor_before_quit is not True:
        why = ((result.owner or {}).get("successor") or {}).get("problems")
        problems.append(
            "the candidate elected no successor while the host still ran"
            + (f": {'; '.join(why)}" if why else "")
        )
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

    O2 is ``=`` when the row's states are the same, the daemon row sent no
    class of signal that neither Direct's construction nor the reference's
    run sends, and neither traced O2 is violated or incomplete where the other
    is not. ``held`` against ``unknown`` is no difference: which of the two a
    row reads depends on whether a recipient outlived the next sample, not on
    what was sent. Two unobserved states compare equal as labels, which says
    nothing of what either row's unobserved actors sent.
    """
    differences = []
    for name in ("o1_single_browser", "o2", "o4_session"):
        if getattr(direct, name) != getattr(daemon, name):
            differences.append(
                f"{name}: Direct {getattr(direct, name)!r}, daemon "
                f"{getattr(daemon, name)!r}"
            )
    if (direct.o2_traced == O2_VIOLATED) != (daemon.o2_traced == O2_VIOLATED):
        differences.append(
            f"o2_traced: Direct {direct.o2_traced!r}, daemon {daemon.o2_traced!r}"
        )
    if direct.oracle_collection != daemon.oracle_collection:
        differences.append(
            f"oracle_collection: Direct {direct.oracle_collection!r}, daemon "
            f"{daemon.oracle_collection!r}"
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
    #: H-R11: this row planted a failing ``IsProcessInJob`` shim.
    failed_job_query: bool = False
    #: H-R11: whether the shim's query was reached, whether a member of another
    #: owned Job was terminated during the routine drain, and the availability.
    job_query_reached: bool | None = None
    job_member_terminated: bool | None = None
    successor_before_quit: bool | None = None


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
    #: What kept the row from observing or cleaning up (H-R11's installer
    #: evidence, its script): also in ``failures``, and checked in every
    #: experiment, a known-bad control included.
    observation_failures: list[str] = field(default_factory=list)
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
        failures += [f"O2 violation: {line}" for line in o2.canary_deaths]
        if o2.required:
            failures += [f"O2 incomplete: {line}" for line in o2.incomplete]

    watcher_problems = watcher_failures(
        observed.watcher,
        actors_began=observed.actors_began,
        actors_ended=observed.actors_ended,
        browser_key=observed.browser_key,
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
        o2=o2.row if o2 is not None else O2_UNOBSERVED,
        o2_traced=o2.state if o2 is not None else O2_UNOBSERVED,
        o2_required=o2.required if o2 is not None else False,
        oracle_collection=o2.collection if o2 is not None else ORACLE_UNAVAILABLE,
        signal_classes=o2.classes if o2 is not None else (),
        guardian_owner_group=killed.get("guardian_owner_group"),
        recovered=_recovered(host) if killed and observed.daemon else None,
        job_query_reached=(
            observed.job_query_reached if observed.failed_job_query else None
        ),
        job_member_terminated=(
            observed.job_member_terminated if observed.failed_job_query else None
        ),
        successor_before_quit=(
            observed.successor_before_quit if observed.failed_job_query else None
        ),
    )
    if observed.failed_job_query and observed.daemon and not observed.job_query_reached:
        # The row exists to fail exactly the query ``_in_another_owned_job``
        # makes; a daemon row that never reached it measured nothing.
        failures.append(
            "the failing Job-membership query was never reached during the drain"
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
    job_query_shim: ShimVenv | None = None,
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
    *job_query_shim* makes it H-R11 (``job_query``): the actors start from that
    venv, whose declared shim fails the routine drain's Job-membership query;
    the browser cache is a row-private one of links; after the read the row
    holds a dependency back so the next call starts an installer that waits on
    a host that never answers, then calls ``close_session`` with it running,
    and once more after, where a successor would serve.
    """
    # First, before anything reads, launches or spawns.
    account = claim_account(profile)

    origin, proxy = egress
    mode = "daemon" if daemon else "direct"
    if expect_owner is None:
        expect_owner = daemon
    result = RowResult(experiment=experiment, mode=mode, reference=reference)
    runtime = runtime or candidate_runtime()
    shim = job_query_shim
    command = list(
        command
        or ([shim.python, "-m", "linkedin_mcp_server"] if shim else runtime.command())
    )

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
    cache: PrivateCache | None = None
    stall: StallHost | None = None
    fates = Fates()
    job_window: dict[str, Any] = {}
    #: H-R11 daemon: the owner the descriptor named after the probe, and why it
    #: is not shown to be a successor that served it (``successor_verdict``).
    successor: dict[str, Any] = {}
    #: The routine drain's reading, taken once the installers have settled.
    drain: DrainReading | None = None
    if shim is not None:
        # Every planted failure of an earlier row in the same venv is not ours.
        shim.reached_file.unlink(missing_ok=True)
        locations = await asyncio.to_thread(install_locations, runtime.python, browsers)
        stall = StallHost().start()
        try:
            cache = await asyncio.to_thread(
                private_install, runtime.python, locations, env, stall
            )
            emit(
                "harness",
                "shim.planted",
                **shim.as_event_fields(),
                private_cache=str(cache.directory),
                linked=[str(location) for location in locations],
                stall_host=stall.url,
            )
        except BaseException:
            try:
                if cache is not None:
                    cache.dismantle()
            finally:
                stall.stop()
            raise
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
    # Constructed here, started inside the row's try: whatever fails from the
    # watcher's start on, its ``finally`` ends every helper already started.
    canaries = Canaries()
    canary_problems: list[str] = []
    oracle = SignalOracle(work_dir, required=kill_actor and ORACLE_REQUIRED)
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
            "required": oracle.required,
        }
        try:
            # SIGKILL on POSIX, TerminateProcess on Windows: psutil's kill().
            victim.kill()
        except psutil.NoSuchProcess:
            killed["exit"] = "gone before the kill"
        except psutil.Error as exc:
            killed["exit"] = f"not killed ({type(exc).__name__})"
        else:
            try:
                dead = await asyncio.to_thread(
                    wait_until_dead, victim, _OWNER_KILL_WAIT_SECONDS
                )
            except psutil.Error as exc:
                killed["exit"] = f"killed, death unconfirmed ({type(exc).__name__})"
            else:
                killed["exit"] = "killed" if dead else "still running after the kill"
        # ``actor`` is the event's own field: the killed role goes as ``role``.
        emit(
            "harness",
            "actor.killed",
            role=killed.get("actor"),
            **{name: value for name, value in killed.items() if name != "actor"},
        )

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

    async def job_query_script(call: ToolCall) -> None:
        """Start an installer, close with it running, then call once more."""
        assert cache is not None
        # The owner whose drain the shim should be reached in: the one serving now.
        job_window["owner_pid"] = identified.pid if identified is not None else None
        held = cache.hold_back()
        # The row's own install record, so setup looks again on the next call.
        (account.auth_root / "browser-install.json").unlink(missing_ok=True)
        emit("harness", "job_query.window", phase="held back", held=str(held))
        await call(READ_TOOL, READ_TOOL_ARGUMENTS)
        members = await asyncio.to_thread(
            wait_for_installers, watcher.observed, watch=fates.watch
        )
        emit(
            "harness",
            "job_query.window",
            phase="installer",
            installers=[list(member) for member in members],
        )
        closed = await call("close_session", {})
        job_window.update(began=closed["began"], ended=closed["ended"])
        emit("harness", "job_query.window", phase="close", **job_window)
        watch_late_installers()
        # Back before the next call, so a successor's setup finds everything
        # and it can serve and later leave through its idle exit; an installer
        # already waiting on the stall host waits on regardless.
        cache.restore()
        # An owner whose close stayed unconfirmed stands down, and a call that
        # reaches it meanwhile is told to call again for its replacement
        # (measured on Windows, run 36384952466: the probe came 33 ms after the
        # verdict, the owner answered "restarting", the host quit, and no
        # successor was ever asked for). So the probe waits for that owner to
        # be gone, observed through the handle the row identified it by.
        leaving = identified is not None and close_left_unconfirmed(
            owner.get("log_path")
        )
        job_window["owner_left_before_probe"] = (
            await asyncio.to_thread(
                wait_until_dead, identified.process, _OWNER_STAND_DOWN_SECONDS
            )
            if leaving and identified is not None
            else None
        )
        probe = await call(READ_TOOL, READ_TOOL_ARGUMENTS)
        job_window["probe_ended"] = probe["ended"]
        job_window["probe"] = {
            name: probe.get(name)
            for name in ("began", "ended", "is_error", "read_the_post")
        }
        emit(
            "harness",
            "job_query.window",
            phase="probe",
            owner_left_before_probe=job_window["owner_left_before_probe"],
            probe_ended=probe["ended"],
            probe_error=probe.get("is_error"),
            probe_read_the_post=probe.get("read_the_post"),
        )
        watch_late_installers()
        if daemon:
            await verify_the_successor(job_window["probe"])
        job_window["script_ended"] = True

    def watch_late_installers() -> None:
        """Watch installer lifetimes the first look missed, while they still run."""
        for pid, start in installer_starts(watcher.observed()):
            fates.watch(pid, start, required=False)

    def find_the_successor(closing: OwnerIdentity | None, close_began: float) -> None:
        """Which owner the descriptor names now, and whether it replaced *closing*."""
        found: OwnerIdentity | None = None
        successor.pop("identify_error", None)
        try:
            published = daemon_descriptor.read(account.auth_root)
        except Exception as exc:  # noqa: BLE001 - the row reports it
            successor["identify_error"] = f"{type(exc).__name__}: {exc}"
            published = None
        if published is not None:
            found, problem = identify_owner(published, account, watcher.observed())
            if problem is not None:
                successor["identify_error"] = problem
            successor.update(pid=published.pid, instance_id=published.instance_id)
        problems = successor_problems(
            watcher.observed(), closing, found, close_began=close_began
        )
        # The watcher's first sight of a process is when it sampled it, not
        # when it began; on Windows the creation time is the kernel's own, on
        # the same clock as the close's start.
        if found is not None and found.create_time < close_began:
            problems.append(
                f"pid {found.pid} was created at {found.create_time}, before the "
                f"close began at {close_began}"
            )
        successor["problems"] = problems

    async def verify_the_successor(probe: Mapping[str, Any]) -> None:
        """Before the host quits: a new owner, and only it, served the probe."""
        left = job_window.get("owner_left_before_probe")
        close_began = job_window.get("began")
        served = not probe.get("is_error") and bool(probe.get("read_the_post"))
        if served and left is True and close_began is not None:
            deadline = time.monotonic() + _SUCCESSOR_SECONDS
            while True:
                await asyncio.to_thread(find_the_successor, identified, close_began)
                if not successor["problems"] or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.2)
        successor["problems"] = successor_verdict(
            probe=probe, left=left, problems=successor.get("problems")
        )
        successor["verified"] = not successor["problems"]
        job_window["successor_verified"] = successor["verified"]
        emit(
            "harness",
            "owner.successor",
            **{name: value for name, value in successor.items()},
        )

    async def after_call() -> None:
        await find_the_owner()
        if kill_actor:
            await kill_the_actor()

    after: ProfileSnapshot | None = None
    actors_ended: float | None = None
    residual: list[int] = []
    teardown: list[str] = []
    try:
        watcher.start()
        # After the watcher's baseline, so it reports the canaries' starts and
        # a signal aimed at one resolves to it.
        for canary in canaries.start():
            emit("canary", "canary.start", **canary.as_event_fields())
        canary_problems = canaries.outside_the_harness()
        emit(
            "harness",
            "signal.oracle",
            phase="setup",
            available=oracle.available,
            reason=oracle.unavailable,
            ptrace_scope=oracle.scope,
            required=oracle.required,
        )
        actors_began = time.time()
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
            script=job_query_script if shim is not None else None,
        )
        result.host = host
        if (kill_actor or shim is not None) and daemon:
            # The owner the frontend recovered to is the one that now has to
            # leave through its idle exit and be cleaned up. If nothing else
            # was published since, the killed owner's own handle stays, so
            # cleanup settles it as gone rather than meeting its descriptor as
            # one it never identified. A different owner that could not be
            # identified keeps its own record: that is the row's finding.
            killed_owner, killed_record = identified, dict(owner)
            owner.clear()
            identified = None
            await find_the_owner()
            # A stale descriptor can still name the killed owner while it is
            # an unreaped zombie, and identifying it again is not a successor.
            again = (
                identified is not None
                and killed_owner is not None
                and (identified.pid, identified.create_time)
                == (killed_owner.pid, killed_owner.create_time)
            )
            replaced = (identified is not None and not again) or owner.get(
                "pid"
            ) not in (None, killed_record.get("pid"))
            if not replaced:
                identified = killed_owner
                owner.clear()
                owner.update(killed_record)
            owner["replaced_after_kill"] = replaced
            if shim is not None:
                owner["successor"] = dict(successor)
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
                exited = await asyncio.to_thread(
                    wait_until_dead,
                    identified.process,
                    IDLE_TIMEOUT_SECONDS + _OWNER_EXIT_SLACK_SECONDS,
                )
            except psutil.Error as exc:
                exit_record["how"] = f"unknown ({type(exc).__name__})"
            else:
                exit_record["how"] = "exited" if exited else "still running"
                if exited:
                    exit_record["seconds_after_quit"] = round(
                        time.monotonic() - began, 3
                    )
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

        if shim is not None:
            # Ended with their Jobs when the server or owner went; then the
            # held-back dependency is put back before anything else runs.
            await asyncio.to_thread(fates.settle, _BROWSER_GONE_SECONDS)
            # Every owner that could drain has exited, so the shim's records
            # are final: the reading, and whether its evidence is complete.
            drain = job_query_reading(
                shim, fates=fates, owner_pid=job_window.get("owner_pid"), daemon=daemon
            )
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
        if cache is not None:
            try:
                cache.dismantle()
            except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
                teardown.append(f"the private browser cache stayed: {exc!r}")
            try:
                # The post-quit session runs on the real cache again.
                await asyncio.to_thread(
                    record_install,
                    runtime.python,
                    {**env, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)},
                )
            except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
                teardown.append(f"the real cache's install was not recorded: {exc!r}")
        if stall is not None:
            stall.stop()
        # Each helper is ended whatever the one before it did; a failure is
        # the row's to report.
        confirmed = [killed["pid"]] if killed.get("exit") == "killed" else []
        try:
            # Once its tracees have exited, strace has seen every signal they
            # sent.
            outcome = await asyncio.to_thread(oracle.stop, confirmed_dead=confirmed)
        except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
            teardown.append(f"the signal oracle could not be stopped: {exc!r}")
            outcome = OracleOutcome(
                status=O2_INCOMPLETE,
                required=oracle.required,
                reasons=[f"the oracle could not be stopped: {exc!r}"],
            )
        try:
            # The row's interval ends here: the watcher stops before anything
            # else starts on the profile.
            result.watcher = watcher.stop()
        except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
            teardown.append(f"the watcher could not be stopped: {exc!r}")
        canary_deaths: list[dict[str, Any]] = []
        try:
            canary_deaths = canaries.deaths()
        finally:
            canaries.stop()
        observed_events = watcher.observed()
        o2 = derive_o2(
            outcome,
            ProcessHistory(observed_events, outside=[os.getpid()]),
            canary_deaths=canary_deaths,
        )
        emit("harness", "signal.oracle", phase="stop", **outcome.as_event_fields())
        for call in outcome.calls:
            emit("harness", "signal.call", **call.as_event_fields())
        for resolved in o2.resolved:
            emit("harness", "signal.resolved", **resolved)
        for death in canary_deaths:
            emit("canary", "process.death_unattributed", **death)
        for violation in o2.violations:
            emit("harness", "signal.violation", violation=violation)
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
                # The declared shim venv is where the actors start from; that
                # it imports the runtime's code was checked when it was made.
                replace(runtime, python=shim.python) if shim else runtime,
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
    observation = job_query_problems(
        shim,
        fates=fates,
        reading=drain,
        window=job_window,
        script_error=host.script_error,
    )
    if shim is not None:
        # An installer whose end was not observed may still be running on the
        # profile's setup, so no session starts after the row until every one
        # of them is an observed exit, and every lifetime the drain asked
        # about is one of them.
        refusals += [f"H-R11 evidence incomplete: {p}" for p in observation]
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
            **job_query_observations(
                shim,
                reading=drain,
                window=job_window,
                owner_pid=job_window.get("owner_pid"),
                daemon=daemon,
            ),
        )
    )
    if shim is not None:
        for fate in fates.fates.values():
            emit("harness", "installer.fate", **fate.as_event_fields())
        emit(
            "harness",
            "shim.reached",
            lines=reached(shim.reached_file),
            terminations=terminations(shim.reached_file),
            shim_sha256=shim.shim_sha256,
        )
        if fates.alive():
            observation.append(
                f"installers outlived the row: {[f.pid for f in fates.alive()]}"
            )
        # A known-bad control keeps its behaviour, never a failure to observe
        # or to clean up after it.
        observation += [f"teardown: {problem}" for problem in teardown]
        result.observation_failures = observation
        result.failures += [p for p in observation if not p.startswith("teardown: ")]
    result.killed = killed or None
    result.o2 = o2
    result.failures += result.runtime_failures
    result.failures += [f"canary placement: {problem}" for problem in canary_problems]
    result.failures += [f"teardown: {problem}" for problem in teardown]
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
        o2_state=o2.row,
        o2_traced=o2.state,
    )
    return result
