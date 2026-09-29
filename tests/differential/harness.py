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
import stat
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

from differential import lease_probe, r7_fault
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
from differential.fault_overlay import events as fault_events
from differential.fault_overlay import (
    publish_activation,
    scenario_problems,
    selection_problems,
)
from differential.job_query import (
    SHIM_SHA256,
    Fates,
    PrivateCache,
    ShimVenv,
    StallHost,
    install_locations,
    logged,
    private_install,
    reached,
    record_install,
)
from differential.job_query_model import (
    BASELINE,
    CANDIDATE,
    SOURCE_MODEL,
    RoutineModel,
    source_sha256,
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
from differential.unconfirmed_close import (
    AFTER_CONFIRMED_CLOSE,
    AFTER_CONSUMPTION,
    BEFORE_CLOSE,
    BEFORE_PRESERVATION,
    BEFORE_QUIT,
    BEFORE_RECOVERY,
    LOCK_FILE,
    Deferral,
    PhaseReading,
    R7Continuation,
    R7Setup,
    SharedReduction,
    UnsettledWorker,
    checkpoint_problems,
    clock_sample,
    early_browsers,
    file_sha256,
    gate,
    lock_association,
    lock_identity,
    open_lifetime,
    published_return,
    r7_environment,
    read_phase,
    realtime_interval,
    retain,
    run_owned,
    settlement_problems,
    shared_reduction,
    wait_for_marker,
)
from differential.unconfirmed_close import NO_RECOVERY as R7_NO_RECOVERY
from differential.unconfirmed_close import POST_SETTLEMENT as R7_POST_SETTLEMENT
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
                    began_monotonic_ns = time.monotonic_ns()
                    called = await client.call_tool_mcp(
                        name, arguments, timeout=_CALL_SECONDS
                    )
                    summary: dict[str, Any] = {
                        "tool": name,
                        "began": began,
                        "ended": time.time(),
                        "began_monotonic_ns": began_monotonic_ns,
                        "ended_monotonic_ns": time.monotonic_ns(),
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


def kernel_start_ticks(
    pid: int, start: float, *, open_process: Callable[[int], Any] = psutil.Process
) -> int | None:
    """When the lifetime (*pid*, *start*) began, in the kernel's clock ticks.

    ``/proc/<pid>/stat``'s start time counts ticks since boot, so two of them
    compare whatever the wall clock did meanwhile, which a create time read
    against ``time.time()`` does not. None off Linux, for a process gone, or
    when the pid was not that lifetime both before and after the read.
    """
    if not sys.platform.startswith("linux"):
        return None

    def same() -> bool:
        try:
            created = open_process(pid).create_time()
        except psutil.Error:
            return False
        return abs(created - start) <= _START_TOLERANCE_SECONDS

    if not same():
        return None
    ticks = _stat_start_ticks(pid)
    return ticks if ticks is not None and same() else None


def _stat_start_ticks(pid: int) -> int | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name is in parentheses and may hold anything; the fields
    # after its last ')' start with the state, field 3, so start time (22)
    # is the 20th of them.
    fields = text[text.rfind(")") + 2 :].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def creation_marker() -> tuple[int | None, float | None]:
    """A process started now, as H-R7 marks its close and its barrier.

    Its start in kernel ticks, where they can be read (Linux): a lifetime
    whose start is later in ticks began after the marker, which no reading
    of the wall clock can say across a clock step. And its creation time as
    psutil reads every lifetime's (``start_identity``), so a lifetime the
    watcher recorded can be ordered against the marker after it is gone.
    None for either that cannot be read.
    """
    try:
        marker = subprocess.Popen(
            [sys.executable, "-I", "-c", "import sys; sys.stdin.read()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None, None
    try:
        ticks = (
            _stat_start_ticks(marker.pid) if sys.platform.startswith("linux") else None
        )
        try:
            created: float | None = psutil.Process(marker.pid).create_time()
        except psutil.Error:
            created = None
        return ticks, created
    finally:
        with contextlib.suppress(OSError, ValueError):
            assert marker.stdin is not None
            marker.stdin.close()
        try:
            marker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            marker.kill()
            marker.wait(timeout=10)


def created_after(pid: int, start: float, marker: int | None) -> bool | None:
    """Whether (*pid*, *start*) began after a marker's ticks; None if unknown.

    The same tick is unknown: either could have come first.
    """
    if marker is None:
        return None
    ticks = kernel_start_ticks(pid, start)
    if ticks is None or ticks == marker:
        return None
    return ticks > marker


def successor_problems(
    observed: Iterable[Mapping[str, Any]],
    closing: OwnerIdentity | None,
    successor: OwnerIdentity | None,
    *,
    probe: tuple[float, float] | None,
    probe_requests: int,
    after_close: Callable[[int, float], bool | None],
    browser_after: Callable[[int, float], bool | None] | None = None,
) -> list[str]:
    """Why *successor* is not shown to have replaced *closing* and served the
    probe; empty when it is.

    A served probe says that some owner answered, not which. The successor
    counts only as a lifetime of its own: another pid and create time and
    another instance than the owner that closed, not yet gone, and begun
    after the close (*after_close*, from kernel start times: when the watcher
    first saw a process says only when it looked).

    And it served *this* probe. The origin saw the feed request while the
    probe ran (*probe* is its call's start and return, *probe_requests* the
    feed requests between them), so a row browser alive then made it. Every
    row browser the watcher could have seen in that interval must descend
    from the successor, one of them begun after the close (or, with
    *browser_after*, after the boundary that asks: H-R7's recovery barrier)
    and seen before the probe returned. One from the owner that closed, or
    one whose ancestry is unknown, leaves the served request unattributed; a
    browser launched only after the response is no evidence for it.
    """
    browser_after = browser_after or after_close
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
    if new.exit_t is not None:
        problems.append(f"pid {successor.pid} had exited before the host quit")
    began_after = after_close(successor.pid, successor.create_time)
    if began_after is False:
        problems.append(f"pid {successor.pid} began before the close")
    elif began_after is None:
        problems.append(
            f"pid {successor.pid}'s start could not be ordered against the close"
        )
    if probe is None:
        return [*problems, "the probe's interval was not recorded"]
    began, ended = probe
    if probe_requests < 1:
        problems.append("the origin saw no feed request while the probe ran")
    now = time.time()
    served = False
    for life in history.lifetimes:
        if not (life.in_row and life.was("browser")):
            continue
        # Any row browser the watcher could have seen while the probe ran.
        if life.first_t > ended + MAX_WATCHER_GAP_SECONDS:
            continue
        if life.exit_t is not None and life.exit_t < began:
            continue
        if history.descends(life, old, now) is True:
            problems.append(
                f"browser {life.pid} of pid {closing.pid}, the owner that closed, "
                f"ran while the probe was served"
            )
        elif history.descends(life, new, now) is not True:
            problems.append(
                f"browser {life.pid} ran while the probe was served, and nothing "
                f"ties it to pid {successor.pid}"
            )
        elif life.first_t <= ended and browser_after(life.pid, life.start) is True:
            served = True
    if not served:
        problems.append(
            f"no browser of pid {successor.pid}, begun after the "
            f"{'close' if browser_after is after_close else 'recovery barrier'}, "
            f"was seen before the probe returned"
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
#: How long the installer family may take to settle once the close is over.
_FAMILY_SETTLE_SECONDS = 30.0
#: The two logger events the shim observes (``job_query``): ``core.close``
#: consuming the drain's False, logged only when the drain did not prove the
#: launch gone and never for an exception on the way (e1ex, P2), and the
#: owner's stand-down.
CONSUMED_FALSE = "consumed-false"
STAND_DOWN = "stand-down"
#: The stand-down of an OWNER whose close left the profile held
#: (``server_role.a_held_profile_means_this_owner_must_go``). An owner also
#: stands down for a setup deadline, which is not this continuation.
HELD_PROFILE_REASON = "the browser did not shut down cleanly, so the profile is held"


def owner_events(
    events: Iterable[Mapping[str, Any]],
    *,
    owner: tuple[int, float] | None,
    event: str,
    after_ns: int | None,
    before_ns: int | None = None,
    reason: str | None = None,
) -> list[dict[str, Any]]:
    """The observed logger events of kind *event* the owner that closed reached.

    That very lifetime (its pid and its own creation time, read once at its
    startup), at a monotonic reading no earlier than *after_ns* and, when
    given, no later than *before_ns*, and with *reason* when given. The
    daemon log is one file per auth root that every owner generation appends
    to, so a line in it names no writer; another generation's event, the
    successor's, or an earlier process's at a reused pid witnesses nothing
    for this owner (review e1ey, E1EY-02).
    """
    if owner is None or type(after_ns) is not int:
        return []
    pid, created = owner
    found = []
    for record in events:
        made, t = record.get("pid_created"), record.get("monotonic_ns")
        if record.get("event") != event or record.get("pid") != pid:
            continue
        if not isinstance(made, (int, float)):
            continue
        if abs(float(made) - created) > _START_TOLERANCE_SECONDS:
            continue
        if type(t) is not int or t < after_ns:
            continue
        if before_ns is not None and t > before_ns:
            continue
        if reason is not None and record.get("reason") != reason:
            continue
        found.append(dict(record))
    return found


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


#: Where a row lifetime's recorded ancestry leads (``Lineage.of``).
INSTALLER = "installer"
BELOW_INSTALLER = "below an installer"
FROM_HARNESS = "from the harness"
UNRESOLVED = "unresolved"


class Lineage:
    """Where each row lifetime's recorded ancestry leads, parent by parent.

    ``installer`` for an installer by its own record (``is_installer``);
    ``below an installer`` when a recorded ancestor is one, whatever the
    lifetime's own command; ``from the harness`` only when every link up to a
    process the harness itself started (*outside*, the harness's own pid) is
    a recorded row lifetime and none of them is an installer; ``unresolved``
    otherwise: a parent the watcher never recorded, a link outside the row,
    or a loop. Measured on Windows (run 36410976409, K2): the owner's release
    gate, its Python child and that child's console host trace, through the
    frontend and its launcher, to the harness pid that also started the
    row's canaries.
    """

    def __init__(
        self, observed: Iterable[Mapping[str, Any]], *, outside: Iterable[int] = ()
    ) -> None:
        records = list(observed)
        self.outside = frozenset(outside) or frozenset({os.getpid()})
        self.history = ProcessHistory(records, outside=self.outside)
        self.installers = installer_starts(records)
        self._memo: dict[tuple[int, float], str] = {}

    def _is_installer(self, life: Lifetime) -> bool:
        return any(
            life.pid == pid and abs(life.start - start) <= _START_TOLERANCE_SECONDS
            for pid, start in self.installers
        )

    def of(self, life: Lifetime) -> str:
        if life.identity in self._memo:
            return self._memo[life.identity]
        answer = UNRESOLVED
        current: Lifetime | None = life
        seen: set[tuple[int, float]] = set()
        while current is not None and current.identity not in seen:
            seen.add(current.identity)
            if self._is_installer(current):
                answer = INSTALLER if current is life else BELOW_INSTALLER
                break
            if not current.in_row:
                break
            if current.ppid in self.outside:
                answer = FROM_HARNESS
                break
            parent = self.history.at(current.ppid, current.first_t)
            if parent is None or parent.start > current.start:
                break
            current = parent
        self._memo[life.identity] = answer
        return answer

    def lifetime(self, pid: Any, created: Any) -> Lifetime | None:
        """The one row lifetime recorded at *pid* with creation time *created*."""
        if not isinstance(pid, int) or not isinstance(created, (int, float)):
            return None
        lives = [
            life
            for life in self.history.lifetimes
            if life.pid == pid
            and life.in_row
            and abs(life.start - float(created)) <= _START_TOLERANCE_SECONDS
        ]
        return lives[0] if len(lives) == 1 else None


def installer_family(
    observed: Iterable[Mapping[str, Any]], fates: Fates, *, outside: Iterable[int] = ()
) -> Callable[[Any, Any], bool]:
    """Whether a lifetime is one of the installer family the row tracked.

    A lifetime the row watched as an installer, or one the watcher recorded
    in the row whose own record or recorded ancestry is an installer
    (``Lineage``). An unresolved ancestry is not the family: it may be, which
    keeps it in the inventory, but it witnesses nothing.
    """
    lineage = Lineage(observed, outside=outside)

    def member(pid: Any, created: Any) -> bool:
        if any(fate.is_lifetime(pid, created) for fate in fates.fates.values()):
            return True
        life = lineage.lifetime(pid, created)
        return life is not None and lineage.of(life) in (INSTALLER, BELOW_INSTALLER)

    return member


def unaccounted_members(
    records: Iterable[Mapping[str, Any]],
    observed: Iterable[Mapping[str, Any]],
    fates: Fates,
    *,
    outside: Iterable[int] = (),
) -> list[str]:
    """Every lifetime the shim saw asked about that the row cannot account for.

    A record names a member of some actor's adopted Job, which is positive
    evidence that the lifetime existed. It is accounted for when the row
    watched it or the watcher recorded it in the row: then either the
    inventory has to see it end (an installer, below one, or of unresolved
    ancestry) or its ancestry leads to the harness with no installer on the
    way. Measured on Windows (run 36410976409, K2): the drain also asked
    about the owner's release gate, its Python child and that child's console
    host. A lifetime nobody recorded could be an installer still running.
    """
    lineage = Lineage(observed, outside=outside)
    problems = []
    seen: set[tuple[Any, Any]] = set()
    for record in records:
        key = (record.get("member"), record.get("created"))
        if key in seen:
            continue
        seen.add(key)
        pid, created = key
        if any(fate.is_lifetime(pid, created) for fate in fates.fates.values()):
            continue
        if lineage.lifetime(pid, created) is not None:
            continue
        problems.append(
            f"the drain asked about pid {pid} created {created}, a lifetime the "
            f"watcher never recorded in the row, so its end is unknown"
        )
    return problems


def family_problems(
    observed: Iterable[Mapping[str, Any]],
    fates: Fates,
    records: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Why the installer family is not shown ended, from everything known now.

    The watcher's history and the row's own handles (``installer_inventory``),
    and every lifetime the shim has positively seen asked about
    (``unaccounted_members``): a lifetime that evidence proves existed and
    nothing shows ended may still be setup's, however it escaped the
    watcher. The same rules at every boundary: before the harness restores
    or probes, and before its teardown touches the cache.
    """
    observed = list(observed)
    return [
        *installer_inventory(observed, fates),
        *unaccounted_members(records, observed, fates),
    ]


def settle_family(
    observed: Callable[[], Iterable[Mapping[str, Any]]],
    fates: Fates,
    records: Callable[[], Iterable[Mapping[str, Any]]],
    seconds: float = _FAMILY_SETTLE_SECONDS,
) -> list[str]:
    """Wait for the installer family to be shown ended; what is not, if not.

    The labelled boundary before the harness restores the row-private cache
    and makes the recovery probe: restoring a dependency under a download
    still running would race it, and a probe made then would be a different
    measurement. The same reconciliation as the barrier after the row
    (``family_problems``), with the shim's records read afresh each time.
    """
    deadline = time.monotonic() + seconds
    while True:
        problems = family_problems(observed(), fates, records())
        if not problems or time.monotonic() >= deadline:
            return problems
        time.sleep(0.2)


def installer_inventory(
    observed: Iterable[Mapping[str, Any]],
    fates: Fates,
    *,
    outside: Iterable[int] = (),
) -> list[str]:
    """Every lifetime setup could have left running that is not shown ended.

    That is every installer, every row lifetime recorded below one whatever
    its own command (a console host, a helper), every lifetime the row
    watched, and every row lifetime whose ancestry is unresolved: its first
    observation cannot prove that it predates setup. Ended is
    an exit observed through the row's own handle, or the watcher seeing
    that lifetime leave the process table; a handle that could not be opened
    or read, or a lifetime nobody saw leave, is neither.
    """
    records = list(observed)
    lineage = Lineage(records, outside=outside)
    gone = [
        (record.get("pid"), record.get("start_identity"))
        for record in records
        if record.get("kind") == "process.exit"
    ]
    inventory: dict[tuple[int, float], str] = {}
    for life in lineage.history.lifetimes:
        if not life.in_row:
            continue
        kind = lineage.of(life)
        if kind in (INSTALLER, BELOW_INSTALLER, UNRESOLVED):
            inventory[(life.pid, life.start)] = kind
    for key in fates.fates:
        if not any(
            key[0] == pid and abs(key[1] - start) <= _START_TOLERANCE_SECONDS
            for pid, start in inventory
        ):
            inventory[key] = "watched"
    problems = []
    for (pid, start), kind in inventory.items():
        fate = next(
            (
                fate
                for fate in fates.fates.values()
                if fate.pid == pid
                and abs(fate.start - start) <= _START_TOLERANCE_SECONDS
            ),
            None,
        )
        if fate is not None and fate.settled:
            continue
        if any(
            gone_pid == pid
            and isinstance(gone_start, (int, float))
            and abs(gone_start - start) <= _START_TOLERANCE_SECONDS
            for gone_pid, gone_start in gone
        ):
            continue
        why = f": {fate.problem}" if fate is not None and fate.problem else ""
        problems.append(
            f"pid {pid} ({kind}), created {start}, was neither seen to exit nor "
            f"settled through its handle{why}"
        )
    return problems


#: How far the wall clock may move apart from the monotonic one, and how far
#: apart two creation times must be to be ordered at all.
_CLOCK_SECONDS = 0.25


class WallClockMarker:
    """The creation time of a process started as the close begins.

    Windows keeps a process's creation time on the wall clock only, so it
    orders two processes only while that clock ran with the monotonic one:
    this records both as the row began and checks them when asked. Creation
    times within ``_CLOCK_SECONDS`` of the marker are not ordered at all.
    """

    def __init__(self) -> None:
        self.began = (time.time(), time.monotonic())
        self.created: float | None = None

    def mark(self) -> None:
        try:
            marker = subprocess.Popen(
                [sys.executable, "-I", "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return
        try:
            with contextlib.suppress(psutil.Error):
                self.created = psutil.Process(marker.pid).create_time()
        finally:
            with contextlib.suppress(OSError, ValueError):
                assert marker.stdin is not None
                marker.stdin.close()
            try:
                marker.wait(timeout=10)
            except subprocess.TimeoutExpired:
                marker.kill()
                marker.wait(timeout=10)

    def held(self) -> bool:
        """Whether the wall clock has kept pace with the monotonic one so far."""
        wall, mono = self.began
        return abs((time.time() - wall) - (time.monotonic() - mono)) <= _CLOCK_SECONDS

    def after(self, pid: int, start: float) -> bool | None:
        """Whether *start* is after the marker; None when that is not known."""
        if self.created is None or not self.held():
            return None
        if start > self.created + _CLOCK_SECONDS:
            return True
        if start < self.created - _CLOCK_SECONDS:
            return False
        return None


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


#: The one file the harness's own restoration writes in the auth root: the
#: install record ``record_install`` puts back for the restored cache.
_RESTORATION_WRITES = frozenset({"browser-install.json"})


def auth_files(root: Path) -> dict[str, str]:
    """Snapshot files and directories without traversing links or reparse points.

    File content is hashed; directories are recorded even when empty. Failed
    enumeration, metadata or content reads stay ``unreadable``, never absence.
    """
    files: dict[str, str] = {}

    def relative(path: Path) -> str:
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return "."

    def kind(path: Path) -> str:
        try:
            metadata = path.lstat()
        except OSError:
            return "unreadable"
        if stat.S_ISLNK(metadata.st_mode) or (
            getattr(metadata, "st_file_attributes", 0)
            & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            return "link"
        if stat.S_ISDIR(metadata.st_mode):
            return "directory"
        return "file" if stat.S_ISREG(metadata.st_mode) else "unreadable"

    def failed(error: OSError) -> None:
        path = Path(error.filename) if error.filename else root
        files[relative(path)] = "unreadable"

    files["."] = kind(root)
    if files["."] != "directory":
        return files
    for directory, names, entries in os.walk(root, onerror=failed, followlinks=False):
        base = Path(directory)
        for name in [*names, *entries]:
            path = base / name
            entry_kind = kind(path)
            key = relative(path)
            if entry_kind != "file":
                files[key] = entry_kind
                if name in names and entry_kind != "directory":
                    names.remove(name)
            else:
                try:
                    files[key] = hashlib.sha256(path.read_bytes()).hexdigest()
                except OSError:
                    files[key] = "unreadable"
    return files


def restoration_changes(
    before: Mapping[str, str], after: Mapping[str, str]
) -> list[str]:
    """What changed in the auth root across the harness's restoration, beyond
    the install record it writes itself.

    The restoration relinks a row-private cache outside the auth root and
    writes that record. Anything else that changed meanwhile was changed by
    something else, and a final snapshot would count it as the product's.
    """
    problems = []
    for name in sorted(before.keys() | after.keys()):
        if name in _RESTORATION_WRITES:
            continue
        if {before.get(name), after.get(name)} & {"unreadable", "link"}:
            problems.append(
                f"the auth root's {name} could not be compared across restoration"
            )
        elif before.get(name) != after.get(name):
            problems.append(
                f"the auth root's {name} changed while the harness restored the cache"
            )
    return problems


def protected_changes(before: ProfileSnapshot, at: ProfileSnapshot) -> list[str]:
    """What the product changed of the protected session by the recovery boundary.

    Read at that checkpoint, before the harness restores anything, so no
    restoration can repair it; a checkpoint, not a watch over the interval.
    Allowed: the cookie file's bytes and names, which the close's export and
    a session refresh rewrite, and the browser's own profile files. Not
    allowed: another login generation, a staged session no longer usable, a
    new quarantine, a missing profile, an artefact that no longer reads.
    """
    problems = []
    if at.generation != before.generation:
        problems.append(
            f"the login generation changed from {before.generation!r} to "
            f"{at.generation!r}"
        )
    if before.li_at_usable and not at.li_at_usable:
        problems.append("the staged session's li_at is no longer usable")
    quarantined = sorted(set(at.quarantine) - set(before.quarantine))
    if quarantined:
        problems.append(f"quarantined: {quarantined}")
    if before.profile_present and not at.profile_present:
        problems.append("the browser profile is gone")
    unreadable = sorted(set(at.unreadable) - set(before.unreadable))
    if unreadable:
        problems.append(f"no longer readable: {unreadable}")
    return problems


def fault_witnesses(
    records: Iterable[Mapping[str, Any]],
    *,
    owner: tuple[int, float] | None,
    family: Callable[[Any, Any], bool],
    interval_ns: tuple[int, int] | None,
) -> list[dict[str, Any]]:
    """The planted failures that witness the intended entry, and only those.

    A witness was written by the owner that closed, that very lifetime (its
    pid and its own creation time, so neither an earlier process at a reused
    pid nor the successor), about a lifetime of the installer family
    (*family*), asking a Job that owner held (its handle), at a time inside
    the close call (*interval_ns*, host and shim monotonic nanoseconds on this
    machine). Wall time is diagnostic only and cannot move a pre-close query
    inside the interval. Each record certifies one invocation, not later calls.
    """
    if owner is None or interval_ns is None:
        return []
    pid, created = owner
    began, ended = interval_ns
    if type(began) is not int or type(ended) is not int or began > ended:
        return []
    found = []
    for record in records:
        made, t = record.get("pid_created"), record.get("monotonic_ns")
        if record.get("pid") != pid or not isinstance(made, (int, float)):
            continue
        if not isinstance(record.get("job"), int):
            continue
        if abs(float(made) - created) > _START_TOLERANCE_SECONDS:
            continue
        if type(t) is not int or not began <= t <= ended:
            continue
        if family(record.get("member"), record.get("created")):
            found.append(dict(record))
    return found


def imported_process_tree(shim: ShimVenv) -> str | None:
    """The process_tree the shim venv's actors import, by content."""
    module = shim.code.get("module")
    if not module:
        return None
    try:
        text = (Path(module).parent / "process_tree.py").read_text(encoding="utf-8")
    except OSError:
        return None
    return source_sha256(text)


def job_query_problems(
    shim: ShimVenv | None,
    *,
    fates: Fates,
    window: Mapping[str, Any],
    script_error: str | None,
    observed: Iterable[Mapping[str, Any]] = (),
    records: Iterable[Mapping[str, Any]] = (),
    host: Sequence[str] = (),
    watcher: Sequence[str] = (),
    cleanup: Sequence[str] = (),
    before_cleanup: Sequence[str] | None = (),
) -> list[str]:
    """What stops H-R11 from observing the row, in any experiment.

    Not the behaviour under test, so K2 is held to all of it too: the row's
    own script; every installer lifetime not shown ended
    (``installer_inventory``), at the recovery boundary, before the teardown
    touched anything (*before_cleanup*, None when that was never
    established) and after the row; every lifetime the shim saw asked about
    that the row cannot account for (``unaccounted_members``); a harness
    restoration that changed more than its own record; the whole of
    ``host_failures``; what the watcher could not observe (*watcher*,
    ``watcher_failures``); what cleanup could not do (*cleanup*,
    ``DaemonCleanup.failures``); and a wall clock that moved.

    What was unresolved before the teardown stays unresolved. An exit observed
    after the harness stops its stall host cannot establish product settlement
    before that intervention; its cause remains unobserved.
    """
    if shim is None:
        return []
    observed = list(observed)
    problems = list(host)
    if script_error is not None:
        problems.append(f"the H-R11 script failed: {script_error}")
    if before_cleanup is None:
        problems.append(
            "the installer family was never shown ended before the teardown began"
        )
    else:
        problems += [f"before cleanup: {p}" for p in before_cleanup]
    problems += [
        f"installer inventory: {p}" for p in installer_inventory(observed, fates)
    ]
    problems += [
        f"installer evidence: {p}"
        for p in unaccounted_members(records, observed, fates)
    ]
    problems += [
        f"before recovery: {p}" for p in window.get("family_before_recovery") or ()
    ]
    problems += [f"restoration: {p}" for p in window.get("restoration_changes") or ()]
    problems += [f"watcher: {problem}" for problem in watcher]
    problems += [f"cleanup: {problem}" for problem in cleanup]
    if not fates.fates:
        problems.append(
            "no installer ran when the row closed, so the Job query had no "
            "member to be asked about"
        )
    if window.get("clock_held") is False:
        problems.append(
            "the wall clock moved apart from the monotonic one by the end of the "
            "close, so no record's time can be placed inside it"
        )
    if window.get("script_ended") is not True:
        problems.append("the H-R11 script did not run to its end")
    return problems


#: No native evidence here says which caller ended an installer lifetime:
#: exit code 1 comes from the routine drain and from a Job's rundown alike.
UNOBSERVED_CAUSE = "unobserved"
NATIVE = "native"
#: The recovery probe was made only once the installer family had settled.
POST_SETTLEMENT = "post-settlement"
#: Direct keeps its installer in its own setup until host quit, so no
#: settlement can come before a probe, and none is made.
NO_RECOVERY = "none: Direct keeps its installer until host quit"


@dataclass(frozen=True)
class NativeContinuation:
    """What one native H-R11 experiment established, as native evidence only.

    Claim map. That the experiment reached the planted situation: the owner
    that closed, the installer family it held, and positive fault witnesses
    (``fault_witnesses``), each certifying one invocation. What followed:
    that same owner lifetime reaching core.close's consumption of the drain's
    False inside the close, then its own held-profile stand-down, and its
    observed exit (K3, ``owner_events``); the family settled at a labelled
    boundary, then a post-settlement recovery whose successor served the probe
    (K3); the protected session at that boundary; and every validity problem.
    The shared daemon log is diagnostic only: its lines name no writer.

    ``termination_cause`` is ``unobserved`` in every cell, and no field says
    whether any caller selected ``TerminateProcess``: that is the source
    model's (``job_query_model``), a different kind of evidence. Nothing
    here is merged with it into a native reading of the drain.
    """

    experiment: str
    run: str
    mode: str
    #: The revision the actors ran (the pin, or the checkout's HEAD), and the
    #: process_tree they import, by content (``source_sha256``).
    revision: str | None
    process_tree_sha256: str | None
    shim_sha256: str
    vector: RowVector | None
    first_read: bool
    #: The owner that closed, (pid, creation time); None in Direct.
    owner: tuple[int, float] | None
    #: The close call, as the host sent it and read its answer.
    close: tuple[float, float] | None
    #: Installer lifetimes the row watched.
    installers: int
    #: Every planted failure recorded in the row, and those that witness the
    #: intended entry.
    reached: int
    witnesses: tuple[Mapping[str, Any], ...]
    #: Daemon rows: the owner that closed reached core.close's consumption of
    #: the drain's False inside its close and its held-profile stand-down
    #: after it began (observed logger events of that lifetime), and was seen
    #: to exit. None in Direct.
    consumed_false: bool | None
    stood_down: bool | None
    owner_left: bool | None
    #: Observed logger events recorded in the row, whoever reached them.
    events: int
    recovery: str
    protected_at_boundary: tuple[str, ...]
    successor_verified: bool | None
    successor_problems: tuple[str, ...]
    #: What kept this experiment from being observed, whatever it is.
    validity: tuple[str, ...]
    termination_cause: str = UNOBSERVED_CAUSE
    evidence: str = NATIVE
    close_monotonic_ns: tuple[int, int] | None = None


def native_continuation(
    *,
    experiment: str,
    run: str,
    daemon: bool,
    identity: Mapping[str, Any],
    shim: ShimVenv,
    vector: RowVector | None,
    host: HostSession,
    window: Mapping[str, Any],
    fates: Fates,
    observed: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    successor: Mapping[str, Any],
    validity: Sequence[str],
) -> NativeContinuation:
    """The row's continuation, from what it recorded; judged elsewhere."""
    owner_pid, owner_created = window.get("owner_pid"), window.get("owner_created")
    owner = (
        (int(owner_pid), float(owner_created))
        if daemon
        and isinstance(owner_pid, int)
        and isinstance(owner_created, (int, float))
        else None
    )
    began, ended = window.get("began"), window.get("ended")
    close = (
        (float(began), float(ended))
        if isinstance(began, (int, float)) and isinstance(ended, (int, float))
        else None
    )
    mono_began, mono_ended = (
        window.get("began_monotonic_ns"),
        window.get("ended_monotonic_ns"),
    )
    close_monotonic_ns = (
        (mono_began, mono_ended)
        if type(mono_began) is int and type(mono_ended) is int
        else None
    )
    witnesses = fault_witnesses(
        records,
        owner=owner,
        family=installer_family(observed, fates),
        interval_ns=close_monotonic_ns,
    )
    began_ns, ended_ns = close_monotonic_ns or (None, None)
    consumed = owner_events(
        events,
        owner=owner,
        event=CONSUMED_FALSE,
        after_ns=began_ns,
        before_ns=ended_ns,
    )
    stood = owner_events(
        events,
        owner=owner,
        event=STAND_DOWN,
        after_ns=began_ns,
        reason=HELD_PROFILE_REASON,
    )
    tool = host.tool or {}
    return NativeContinuation(
        experiment=experiment,
        run=run,
        mode="daemon" if daemon else "direct",
        revision=identity.get("head"),
        process_tree_sha256=imported_process_tree(shim),
        shim_sha256=shim.shim_sha256,
        vector=vector,
        first_read=bool(tool)
        and not tool.get("is_error")
        and bool(tool.get("read_the_post")),
        owner=owner,
        close=close,
        close_monotonic_ns=close_monotonic_ns,
        installers=len(fates.fates),
        reached=len(records),
        witnesses=tuple(witnesses),
        consumed_false=bool(consumed) if daemon else None,
        stood_down=bool(stood) if daemon else None,
        owner_left=window.get("owner_left_before_probe") if daemon else None,
        events=len(events),
        recovery=str(window.get("recovery") or "not reached"),
        protected_at_boundary=tuple(window.get("protected_at_boundary") or ()),
        successor_verified=successor.get("verified") if daemon else None,
        successor_problems=tuple(successor.get("problems") or ()),
        validity=tuple(validity),
    )


def continuation_problems(
    continuation: NativeContinuation | None,
    *,
    experiment: str,
    revision: str | None,
    run: str | None = None,
) -> list[str]:
    """The common validity gate of an H-R11 cell, and what its experiment adds.

    Every cell: the expected experiment, mode, revision and shim, a named
    process_tree, a first read of the synthetic post, labels that claim no
    more than native evidence, and not one validity problem (host, watcher,
    cleanup, census, installer family, script, runtime, restoration, clock).
    K1 is the reference: Direct has no adopted Job, so no planted failure is
    required, and one reached there is the wrong topology. K2 and K3: the
    owner that closed and a positive fault witness inside its close. K3 also:
    core.close's negative consumption, the stand-down, the owner's exit, a
    post-settlement recovery with nothing protected changed by its boundary,
    and a successor that served it. No cell says which caller ended an
    installer.
    """
    if continuation is None:
        return [f"{experiment} left no native continuation"]
    c = continuation
    problems = list(c.validity)
    if c.experiment != experiment:
        problems.append(f"the continuation is {c.experiment}'s, not {experiment}'s")
    if run is not None and c.run != run:
        problems.append(f"the continuation is from run {c.run}, not {run}")
    if revision is None or c.revision != revision:
        problems.append(f"the actors ran {c.revision}, not {revision}")
    if c.shim_sha256 != SHIM_SHA256:
        problems.append(f"the shim was {c.shim_sha256}, not the declared {SHIM_SHA256}")
    if c.process_tree_sha256 is None:
        problems.append("the process_tree the actors import could not be read")
    if c.evidence != NATIVE or c.termination_cause != UNOBSERVED_CAUSE:
        problems.append(
            f"the continuation claims {c.evidence!r} evidence and a termination "
            f"cause {c.termination_cause!r}; nothing native observed the caller"
        )
    expected_mode = "direct" if experiment == "K1" else "daemon"
    if c.mode != expected_mode:
        problems.append(f"{experiment} ran in {c.mode} mode, not {expected_mode}")
    if not c.first_read:
        problems.append("the first call did not read the synthetic post")
    if experiment == "K1":
        if c.reached:
            problems.append(
                f"the Direct reference reached the Job-membership query "
                f"{c.reached} time(s), but it has no adopted Job to reach it "
                f"through"
            )
        return problems
    if c.owner is None:
        problems.append("the owner that closed was never identified")
    if c.close is None:
        problems.append("the close's interval was not recorded")
    if not c.witnesses:
        problems.append(
            f"no planted failure witnesses the entry: {c.reached} recorded, none "
            f"by the owner that closed, about the installer family, inside its "
            f"close"
        )
    if experiment == "K2":
        return problems
    if c.consumed_false is not True:
        problems.append(
            f"the owner that closed was not seen to reach core.close's consumption "
            f"of the drain's False inside its close ({c.events} logger event(s) "
            f"recorded in the row)"
        )
    if c.stood_down is not True:
        problems.append(
            f"the owner that closed was not seen to reach its held-profile "
            f"stand-down ({c.events} logger event(s) recorded in the row)"
        )
    if c.owner_left is not True:
        problems.append(
            f"the owner that closed was not seen to exit (owner_left={c.owner_left!r})"
        )
    if c.recovery != POST_SETTLEMENT:
        problems.append(f"no post-settlement recovery: {c.recovery}")
    problems += [f"by the recovery boundary: {p}" for p in c.protected_at_boundary]
    if c.successor_verified is not True:
        problems.append(
            "no successor is shown to have served the recovery"
            + (f": {'; '.join(c.successor_problems)}" if c.successor_problems else "")
        )
    return problems


class R11Ledger:
    """The native continuations of one invocation of the row module.

    Made by that module for itself and emptied by the composition, so a cell
    of another invocation or an earlier repetition never stands in. A second
    continuation for one experiment is refused, not chosen between.
    """

    def __init__(self, run: str) -> None:
        self.run = run
        self._cells: dict[str, NativeContinuation] = {}
        self._problems: list[str] = []

    def record(self, continuation: NativeContinuation | None) -> None:
        if continuation is None:
            return
        if continuation.experiment in self._cells:
            self._problems.append(
                f"a second {continuation.experiment} continuation in one invocation"
            )
            return
        self._cells[continuation.experiment] = continuation

    def take(self) -> tuple[dict[str, NativeContinuation], list[str]]:
        cells, problems = self._cells, self._problems
        self._cells, self._problems = {}, []
        return cells, problems


def r11_composition(
    model: RoutineModel | None,
    ledger: R11Ledger,
    *,
    revisions: Mapping[str, str | None],
) -> list[str]:
    """What stops H-R11's claim from being composed in this invocation.

    A composition of separate results, never a sum of them: the source
    model's conditional branch, calibrated in this process against the exact
    sources the native runtimes imported (the baseline's prohibited
    selection, the candidate's abstention and every positive control); each
    native continuation of this run through the common gate; and K3 no worse
    than K1 on O1, O2 and O4. A missing calibration or cell fails it: K1 and
    K3 selected alone compose nothing. Whole-system O2 stays what the vectors
    say, unobserved where nothing traced it.
    """
    cells, problems = ledger.take()
    if model is None:
        problems.append("no source-model calibration ran in this invocation")
    else:
        if model.evidence != SOURCE_MODEL:
            problems.append(f"the calibration is {model.evidence!r}, not source-model")
        problems += [f"source model: {problem}" for problem in model.problems]
    for experiment in ("K1", "K2", "K3"):
        cell = cells.get(experiment)
        problems += [
            f"{experiment}: {problem}"
            for problem in continuation_problems(
                cell,
                experiment=experiment,
                revision=revisions.get(experiment),
                run=ledger.run,
            )
        ]
        if cell is None or model is None:
            continue
        modelled = model.sha256.get(CANDIDATE if experiment == "K3" else BASELINE)
        if cell.process_tree_sha256 != modelled:
            problems.append(
                f"{experiment}: the actors imported process_tree "
                f"{cell.process_tree_sha256}, the source model ran {modelled}"
            )
    reference, candidate = cells.get("K1"), cells.get("K3")
    if reference is not None and candidate is not None:
        if reference.vector is None or candidate.vector is None:
            problems.append("K1 or K3 left no vector to compare")
        else:
            problems += [
                f"K3 differs from K1 frozen: {difference}"
                for difference in compare_to_direct(reference.vector, candidate.vector)
            ]
    return problems


# --- Row H-R7: the processes around an unconfirmed close ------------------------

#: How long the original owner and its guardian may take to go after an
#: unconfirmed close: the owner's own stand-down bound, and slack.
_R7_EXIT_SECONDS = 60.0


def exit_state(process: Any, seconds: float) -> str:
    """Whether *process* is seen gone within *seconds*: ``exited``, ``still
    running`` or ``unknown``. It waits and sends nothing."""
    if process is None:
        return "unknown: no handle to it was taken"
    try:
        return "exited" if wait_until_dead(process, seconds) else "still running"
    except psutil.Error as exc:
        return f"unknown ({type(exc).__name__})"


def lifetime_exit_state(
    observed: Iterable[Mapping[str, Any]],
    pid: int,
    seconds: float,
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> str:
    """``exit_state`` of the row lifetime the watcher recorded at *pid*.

    A pid that names no process, or another lifetime than every one recorded
    there, shows that lifetime gone: a pid is not reused while its process,
    or its zombie, still exists.
    """
    starts = [
        float(entry["start_identity"])
        for entry in observed
        if entry.get("kind") in ("process.start", "process.update")
        and entry.get("pid") == pid
        and entry.get("in_row") is True
        and isinstance(entry.get("start_identity"), (int, float))
    ]
    if not starts:
        return "unknown: the watcher never recorded it in the row"
    return _lifetime_exit(pid, starts, seconds, open_process=open_process)


def _lifetime_exit(
    pid: int,
    starts: Sequence[float],
    seconds: float,
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> str:
    """``exit_state`` of the lifetime at *pid* that began at one of *starts*.

    A pid that names no process, or another lifetime than every one of
    them, shows that lifetime gone: a pid is not reused while its process,
    or its zombie, still exists.
    """
    try:
        process = open_process(pid)
        created = process.create_time()
    except psutil.NoSuchProcess:
        return "exited"
    except psutil.Error as exc:
        return f"unknown ({type(exc).__name__})"
    if not any(abs(created - start) <= _START_TOLERANCE_SECONDS for start in starts):
        return "exited"
    return exit_state(process, seconds)


class UnresolvedPublication:
    """An owner published on the row's own auth root that the row could not
    tie to a lifetime it identified: *pid* is the one the descriptor named,
    None when the descriptor could not be read.

    Nothing here is signalled: the row never showed that process to be its
    own. ``check`` answers settled only on lifetime evidence that whatever
    published *pid* is gone: no process holds that pid, or the lifetime
    first read there, by pid and create time, has since ended
    (``_lifetime_exit``, one look and no wait, since nothing was sent). The
    first read is taken at once and again by each check until one succeeds;
    whatever published *pid* was either that lifetime or already gone. The
    descriptor settles nothing, missing or replaced: it says nothing of the
    process it named. Nor does a pid whose process cannot be read, and an
    unreadable descriptor names no pid to read, so that one is never settled.
    """

    def __init__(
        self, pid: int | None, *, open_process: Callable[[int], Any] = psutil.Process
    ) -> None:
        self.pid = pid
        self._open = open_process
        self.created: float | None = None
        self.gone = False
        if pid is not None:
            self._first_read()

    def _first_read(self) -> None:
        assert self.pid is not None
        try:
            self.created = self._open(self.pid).create_time()
        except psutil.NoSuchProcess:
            self.gone = True
        except psutil.Error:
            pass

    def check(self, grace: float) -> bool:
        if self.pid is None:
            return False
        if not self.gone and self.created is None:
            self._first_read()
        if not self.gone and self.created is not None:
            state = _lifetime_exit(
                self.pid, [self.created], 0.0, open_process=self._open
            )
            self.gone = state == "exited"
        return self.gone


def r7_continuation(
    setup: R7Setup,
    *,
    window: Mapping[str, Any],
    experiment: str,
    run: str,
    daemon: bool,
    identity: Mapping[str, Any],
    fault_dir: Path | None,
    activation: Mapping[str, Any] | None,
    runtime: Runtime,
    env: Mapping[str, str],
    host: HostSession,
    vector: RowVector | None,
    phase: PhaseReading | None,
    validity: Sequence[str],
    observed: Iterable[Mapping[str, Any]] = (),
    shared: SharedReduction = SharedReduction(),
) -> R7Continuation:
    """The row's H-R7 continuation, from what it recorded; judged elsewhere.

    The selected call is judged here, once every actor has exited, from the
    fault's own records: a duplicate or an invalid entry written late counts.
    The process_tree is the one the actors import, by content, and the fault
    text the one in the overlay now, not the one declared. So is early use
    of the profile: against the whole of *observed*, the watcher's completed
    history, wherever the row reached its recovery barrier, so a browser the
    watcher reported after the barrier's own look still counts.
    """
    overlay = setup.overlay
    if overlay is not None:
        tree = (overlay.reports.get("overlay isolated") or {}).get("process_tree")
        try:
            fault: str | None = hashlib.sha256(
                overlay.fault_file.read_bytes()
            ).hexdigest()
        except OSError:
            fault = None
    else:
        tree = str(runtime.checkout / "linkedin_mcp_server" / "process_tree.py")
        fault = None
    consumed: int | None = None
    if fault_dir is not None:
        consumed = sum(
            1
            for event in fault_events(fault_dir)
            if event.get("event") == r7_fault.CONSUMED
        )
    selection: list[str] = []
    if setup.activate:
        if activation is None or fault_dir is None:
            selection = ["no activation was published"]
        else:
            selection = selection_problems(
                fault_dir,
                activation=activation,
                sent_ns=(window.get("close") or {}).get("began_monotonic_ns"),
            )
    tool = host.tool or {}
    principal, guardian, lock = (
        window.get("principal"),
        window.get("guardian"),
        window.get("lock"),
    )
    early: list[str] = []
    if "barrier_created" in window and principal:
        early = early_browsers(
            observed,
            (int(principal[0]), float(principal[1])),
            since=window.get("close_created"),
            until=window.get("barrier_created"),
        )
    return R7Continuation(
        experiment=experiment,
        repetition=setup.repetition,
        run=run,
        mode="daemon" if daemon else "direct",
        control=setup.control,
        revision=identity.get("head"),
        process_tree_sha256=file_sha256(tree),
        fault_sha256=fault,
        scenario=tuple(scenario_problems(env)),
        vector=vector,
        first_read=bool(tool)
        and not tool.get("is_error")
        and bool(tool.get("read_the_post")),
        principal=(int(principal[0]), float(principal[1])) if principal else None,
        role=window.get("role"),
        guardian=(int(guardian[0]), float(guardian[1])) if guardian else None,
        guardian_group=window.get("guardian_group"),
        owner_group=window.get("owner_group"),
        marker_digest=window.get("marker_digest"),
        lock=(int(lock[0]), int(lock[1])) if lock else None,
        checkpoints=tuple(window.get("checkpoints") or ()),
        traced_before_activation=window.get("traced_before_close"),
        activated=activation is not None,
        selection=tuple(selection),
        consumed=consumed,
        owner_exit=window.get("owner_exit"),
        guardian_exit=window.get("guardian_exit"),
        pre_probe=tuple(window.get("pre_probe") or ()),
        recovery=str(window.get("recovery") or "not reached"),
        successor_verified=window.get("successor_verified"),
        successor_problems=tuple(window.get("successor_problems") or ()),
        ended_by_harness=tuple(window.get("ended_by_harness") or ()),
        phase=phase,
        validity=tuple(validity),
        shared=shared,
        early_use=tuple(early),
    )


def r7_settled_problems(window: Mapping[str, Any], *, daemon: bool) -> list[str]:
    """Why H-R7's actors are not all shown gone after the row.

    Direct: the server and its guardian, after the host's quit. Daemon:
    every owner the harness ended, and its guardian. A process that could
    not be ended or seen gone is not settled, however the row went.
    """
    problems = []
    if not daemon:
        for name, key in (
            ("server", "server_exit"),
            ("guardian", "guardian_after_quit"),
        ):
            if window.get(key) != "exited":
                problems.append(
                    f"the Direct {name} was {window.get(key)!r} after the quit"
                )
        return problems
    for record in window.get("ended_by_harness") or []:
        if record.get("result") not in ("gone", "stopped"):
            problems.append(
                f"the {record.get('who')} {record.get('pid')} was {record.get('result')!r}"
            )
        if record.get("guardian_exit") not in ("exited", "none was seen"):
            problems.append(
                f"the {record.get('who')}'s guardian {record.get('guardian')} was "
                f"{record.get('guardian_exit')!r}"
            )
    return problems


def is_alive(process: Any) -> bool | None:
    """Whether *process* still runs, a zombie that has not wholly exited
    included; None when that cannot be read."""
    if process is None:
        return None
    try:
        return not is_dead(process)
    except psutil.Error:
        return None


def end_owner(identity: OwnerIdentity) -> str:
    """Cleanup after measurement only: end the owner this row identified.

    Through the handle taken when the row identified it, which psutil checks
    against a reused pid before it signals; never by a pid looked up again.
    ``gone`` when it had already exited, ``stopped`` when it was killed and
    seen gone, anything else unknown.
    """
    try:
        if not identity.process.is_running() or is_dead(identity.process):
            return "gone"
        identity.process.kill()
    except psutil.NoSuchProcess:
        return "gone"
    except psutil.Error as exc:
        return f"unknown ({type(exc).__name__})"
    try:
        dead = wait_until_dead(identity.process, _OWNER_KILL_WAIT_SECONDS)
    except psutil.Error as exc:
        return f"unknown ({type(exc).__name__})"
    return "stopped" if dead else "still running"


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
    #: H-R11: what the native experiment established, and every problem that
    #: kept it from being observed (``continuation_problems`` judges it).
    continuation: NativeContinuation | None = None
    #: H-R7: the same for an unconfirmed close (``unconfirmed_close.r7_problems``).
    unconfirmed: R7Continuation | None = None
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
    unconfirmed_close: R7Setup | None = None,
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
    a host that never answers, then calls ``close_session`` with it running.
    In daemon mode, once the installer family has settled, the row restores
    the cache and calls once more, where a successor would serve. What the
    row established is ``RowResult.continuation`` (``NativeContinuation``).
    *unconfirmed_close* makes it H-R7 (``unconfirmed_close``): the actors start
    from the setup's fault overlay with the idle close off; after the read the
    row identifies the original actor, its guardian, the launch marker and the
    profile lock, attaches the trace, activates the fault and sends
    ``close_session``, then takes the lease checkpoints and, in daemon mode,
    recovers once the original owner and guardian are gone and the lock is
    free. Whatever still serves is ended after measurement, since nothing
    idles out, and that settling and the teardown after it run whole even
    when the row is cancelled, the cancellation raised only once they are
    done. What it established is ``RowResult.unconfirmed``.

    No row starts while anything an earlier row left is unsettled
    (``unconfirmed_close.settlement_problems``).
    """
    r7 = unconfirmed_close
    # Before anything else, whichever row this is: an earlier row's worker or
    # helper still running could still be asking about a profile, and a
    # tracer, owner or guardian it retained could still act in this one.
    left = settlement_problems()
    if left:
        raise UnsettledWorker(f"an earlier row left these unsettled: {left}")
    # First, before anything reads, launches or spawns.
    account = claim_account(profile)

    origin, proxy = egress
    mode = "daemon" if daemon else "direct"
    if expect_owner is None:
        expect_owner = daemon
    result = RowResult(experiment=experiment, mode=mode, reference=reference)
    runtime = runtime or candidate_runtime()
    shim = job_query_shim
    overlay = r7.overlay if r7 is not None else None
    if shim is not None:
        default_command = [shim.python, "-m", "linkedin_mcp_server"]
    elif overlay is not None:
        default_command = [overlay.python, "-m", "linkedin_mcp_server"]
    else:
        default_command = runtime.command()
    command = list(command or default_command)

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
    #: H-R7: the fault's row directory, fresh for this execution, which only an
    #: overlay's actors are told about.
    fault_dir: Path | None = None
    if r7 is not None:
        if overlay is not None:
            fault_dir = work_dir / "fault"
            # Not exist_ok: a directory another execution wrote would lend it
            # a claim or an activation.
            fault_dir.mkdir()
        env = r7_environment(env, fault_dir=fault_dir)
    cache: PrivateCache | None = None
    stall: StallHost | None = None
    fates = Fates()
    job_window: dict[str, Any] = {}
    #: H-R11 daemon: the owner the descriptor named after the probe, and why it
    #: is not shown to be a successor that served it (``successor_verdict``).
    successor: dict[str, Any] = {}
    #: H-R11 daemon: orders the successor's creation after the close.
    close_clock = WallClockMarker()
    #: H-R11: why the installer family is not shown ended, taken after host
    #: quit and before the teardown; None until that verdict has been taken.
    family_at_teardown: list[str] | None = None
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
    oracle = SignalOracle(
        work_dir, required=(kill_actor or r7 is not None) and ORACLE_REQUIRED
    )
    actors_began = time.time()

    owner: dict[str, Any] = {}
    identified: OwnerIdentity | None = None
    killed: dict[str, Any] = {}
    server: dict[str, int] = {}
    #: H-R7: what the row recorded, all of it fit for the packet, and the
    #: handles it took, which are not: each is used to wait on its process, and
    #: an owner's to end it after measurement.
    r7_window: dict[str, Any] = {
        "checkpoints": [],
        "clocks": [],
        "ended_by_harness": [],
        "script_problems": [],
    }
    if overlay is not None:
        # The overlay's own identity, kept apart from the runtime's (the
        # revision in ``identity.json``): what the actors started from.
        r7_window["overlay"] = {
            "python": overlay.python,
            "source_python": overlay.source_python,
            "source_purelib": overlay.source_purelib,
            "fault_sha256": overlay.fault_sha256,
            "pth_sha256": overlay.pth_sha256,
        }
    r7_handles: dict[str, Any] = {}
    activation: dict[str, Any] | None = None
    #: The owner cleanup settles: whoever the descriptor names at the end.
    cleanup_owner: OwnerIdentity | None = None
    #: H-R7: the cancellations its teardown held back (``Deferral``), raised
    #: once that teardown is done, and whether ``r7_after_quit`` has begun: a
    #: row cancelled before it still settles its owners in the teardown.
    r7_defer = Deferral()
    r7_settling_began = False
    #: H-R7: each owner published on the row's root that it could not
    #: identify, by the pid named and the lifetime first read there.
    r7_publications: set[tuple[int | None, float | None]] = set()

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
        """Start an installer and close with it running; in daemon mode, once
        the installer family has settled, recover and call once more."""
        assert cache is not None and shim is not None
        # The owner whose drain the shim should be reached in: the one serving now.
        job_window["owner_pid"] = identified.pid if identified is not None else None
        job_window["owner_created"] = (
            identified.create_time if identified is not None else None
        )
        log_path = owner.get("log_path")
        job_window["owner_log"] = log_path
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
        # Whatever was created after this marker was created after the close
        # began, while the wall clock kept pace (``WallClockMarker``).
        await asyncio.to_thread(close_clock.mark)
        closed = await call("close_session", {})
        job_window.update(
            began=closed["began"],
            ended=closed["ended"],
            began_monotonic_ns=closed.get("began_monotonic_ns"),
            ended_monotonic_ns=closed.get("ended_monotonic_ns"),
        )
        job_window["clock_held"] = close_clock.held()
        # Whether the owner that closed reached core.close's consumption of the
        # drain's False inside the close: the shim records it synchronously in
        # that owner before the call answers. Only a lifetime-bound event
        # counts; the daemon log is shared by every owner generation.
        consumed = daemon and bool(
            owner_events(
                logged(shim.reached_file),
                owner=(
                    (identified.pid, identified.create_time)
                    if identified is not None
                    else None
                ),
                event=CONSUMED_FALSE,
                after_ns=closed.get("began_monotonic_ns"),
                before_ns=closed.get("ended_monotonic_ns"),
            )
        )
        job_window["consumed_false"] = consumed
        emit("harness", "job_query.window", phase="close", **job_window)
        watch_late_installers()
        if not daemon:
            # Direct's own setup holds the installer until host quit, so its
            # family settles only then (``fates.settle`` below) and no probe
            # could come after that settlement.
            job_window["recovery"] = NO_RECOVERY
            job_window["script_ended"] = True
            return
        # An owner whose close stayed unconfirmed stands down, and a call that
        # reaches it meanwhile is told to call again for its replacement
        # (measured on Windows, run 36384952466: the probe came 33 ms after the
        # verdict, the owner answered "restarting", the host quit, and no
        # successor was ever asked for). So the probe waits for that owner to
        # be gone, observed through the handle the row identified it by.
        leaving = identified is not None and consumed is True
        job_window["owner_left_before_probe"] = (
            await asyncio.to_thread(
                wait_until_dead, identified.process, _OWNER_STAND_DOWN_SECONDS
            )
            if leaving and identified is not None
            else None
        )
        # The labelled boundary: the installer family settled first, then the
        # harness's restoration, then the probe, so the probe is a
        # post-settlement recovery and the restoration races no download.
        # Settled means every lifetime known so far, the shim's positively
        # queried ones included, not only what the watcher recorded.
        watch_late_installers()
        unsettled = await asyncio.to_thread(
            settle_family,
            watcher.observed,
            fates,
            lambda: reached(shim.reached_file),
            _FAMILY_SETTLE_SECONDS,
        )
        job_window["family_before_recovery"] = unsettled
        emit("harness", "job_query.window", phase="family settled", unsettled=unsettled)
        if unsettled:
            job_window["recovery"] = "not made: the installer family had not settled"
            job_window["script_ended"] = True
            return
        # What the product left at the boundary, read before the harness
        # restores anything, and the auth root on both sides of that
        # restoration, so neither can stand for the other.
        at_boundary = snapshot(account.profile, expected_digest=staged.li_at_digest)
        job_window["protected_at_boundary"] = protected_changes(before, at_boundary)
        unrestored = await asyncio.to_thread(auth_files, account.auth_root)
        # Both the held-back link and its install record must be ready before
        # the next owner can serve a browser-backed read.
        await asyncio.to_thread(cache.restore_installed, runtime.python, env)
        restored = await asyncio.to_thread(auth_files, account.auth_root)
        job_window["restoration_changes"] = restoration_changes(unrestored, restored)
        job_window["recovery"] = POST_SETTLEMENT
        emit(
            "harness",
            "job_query.window",
            phase="cache restored",
            at_boundary=at_boundary.as_event_fields(),
            protected_at_boundary=job_window["protected_at_boundary"],
            restoration_changes=job_window["restoration_changes"],
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
        await verify_the_successor(job_window["probe"])
        job_window["script_ended"] = True

    def watch_late_installers() -> None:
        """Watch installer lifetimes the first look missed; one that cannot be
        watched stays an unknown fate for the inventory to account for."""
        for pid, start in installer_starts(watcher.observed()):
            fates.watch(pid, start)

    def find_the_successor(
        closing: OwnerIdentity | None, probe: Mapping[str, Any]
    ) -> None:
        """Which owner the descriptor names now, and whether it replaced *closing*
        and served *probe*."""
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
        began, ended = probe.get("began"), probe.get("ended")
        interval = (began, ended) if began is not None and ended is not None else None
        successor["problems"] = successor_problems(
            watcher.observed(),
            closing,
            found,
            probe=interval,
            probe_requests=sum(
                1
                for request in feed_requests(origin.requests[request_mark:])
                if interval is not None
                and interval[0] <= (request.t or 0.0) <= interval[1]
            ),
            after_close=close_clock.after,
        )

    async def verify_the_successor(probe: Mapping[str, Any]) -> None:
        """Before the host quits: a new owner, and only it, served the probe."""
        left = job_window.get("owner_left_before_probe")
        close_began = job_window.get("began")
        served = not probe.get("is_error") and bool(probe.get("read_the_post"))
        if served and left is True and close_began is not None:
            deadline = time.monotonic() + _SUCCESSOR_SECONDS
            while True:
                await asyncio.to_thread(find_the_successor, identified, probe)
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

    lock_path = account.auth_root / LOCK_FILE

    async def lease_checkpoint(
        label: str,
        *,
        holder: int | None = None,
        alive: Mapping[str, bool] | None = None,
    ) -> dict[str, Any]:
        """Ask the non-announcing contender about the lock, once settled.

        On the lock file the row identified, by device and inode now and in
        the contender's own open; for a held one, whether the original actor
        holds it; and whether each named process is alive. A contender that
        fails is recorded and raised: the row advances no further.
        """
        point: dict[str, Any] = {"label": label, "t": time.time()}
        expected = tuple(r7_window["lock"]) if r7_window.get("lock") else None
        try:
            gate(label)
            now = lock_identity(lock_path)
            answer = await run_owned(
                f"lease probe: {label}",
                lease_probe.run_probe,
                str(lock_path),
                seconds=60.0,
            )
            point.update(state=answer.get("state"), reason=answer.get("reason"))
            point["same_lock"] = (
                expected is not None
                and now == expected
                and (answer.get("device"), answer.get("inode")) == expected
            )
            if holder is not None:
                point["association"] = await run_owned(
                    f"lock holder: {label}",
                    lock_association,
                    expected,
                    holder,
                    seconds=30.0,
                )
            if alive:
                point["expect_alive"] = dict(alive)
                point["alive"] = {
                    name: is_alive(r7_handles.get(name)) for name in alive
                }
        except Exception as exc:
            point["error"] = f"{type(exc).__name__}: {exc}"
            r7_window["checkpoints"].append(point)
            emit("harness", "r7.lease", **point)
            raise
        r7_window["checkpoints"].append(point)
        emit("harness", "r7.lease", **point)
        return point

    def hold_publication(pid: int | None, why: str) -> None:
        """Keep an owner published on the row's root that the row could not
        identify (``UnresolvedPublication``) until it is shown gone: no
        later measurement starts meanwhile, and nothing is sent to it."""
        held = UnresolvedPublication(pid)
        key = (pid, held.created)
        if key in r7_publications:
            return
        r7_publications.add(key)
        record = {"pid": pid, "created": held.created, "gone": held.gone, "why": why}
        r7_window.setdefault("unresolved_publications", []).append(record)
        emit("harness", "r7.window", phase="unresolved publication", **record)
        retain(
            f"the owner published on the row's root as pid {pid}, which the row "
            f"could not identify ({why})"
            if pid is not None
            else f"the owner published on the row's root, whose descriptor could "
            f"not be read ({why})",
            held.check,
        )

    def find_the_successor_r7(
        probe: Mapping[str, Any],
    ) -> tuple[OwnerIdentity | None, list[str]]:
        """The owner the descriptor names now, and why it is not shown to be
        a new lifetime that replaced the original and served *probe*: the
        owner begun after the close, since an early election is allowed, and
        the browser that served begun after the recovery barrier, since early
        use of the profile is not. One the row cannot identify, or cannot
        read, is held (``hold_publication``)."""
        found: OwnerIdentity | None = None
        problems: list[str] = []
        try:
            published = daemon_descriptor.read(account.auth_root)
        except Exception as exc:  # noqa: BLE001 - the row reports it
            hold_publication(None, f"{type(exc).__name__}: {exc}")
            return None, [f"the descriptor could not be read: {exc!r}"]
        if published is not None:
            found, problem = identify_owner(published, account, watcher.observed())
            if problem is not None:
                problems.append(problem)
            if found is None:
                hold_publication(published.pid, problem or "not identified")
        began, ended = probe.get("began"), probe.get("ended")
        interval = (began, ended) if began is not None and ended is not None else None
        ticks = r7_window.get("close_ticks")
        barrier = r7_window.get("barrier_ticks")
        problems += successor_problems(
            watcher.observed(),
            identified,
            found,
            probe=interval,
            probe_requests=sum(
                1
                for request in feed_requests(origin.requests[request_mark:])
                if interval is not None
                and interval[0] <= (request.t or 0.0) <= interval[1]
            ),
            after_close=lambda pid, start: created_after(pid, start, ticks),
            browser_after=lambda pid, start: created_after(pid, start, barrier),
        )
        return found, problems

    async def r7_script(call: ToolCall) -> None:
        """Identify, trace, activate, close; then the checkpoints and, for an
        injected owner, the post-settlement recovery."""
        nonlocal activation
        assert r7 is not None
        w = r7_window
        role = "owner" if daemon else "direct"
        w["role"] = role
        if daemon:
            if identified is None:
                w["script_problems"].append("the owner was never identified")
                return
            principal = (identified.pid, identified.create_time)
            r7_handles["original actor"] = identified.process
        else:
            process, created = await run_owned(
                "associate the server",
                associate_server,
                server.get("pid", -1),
                watcher.observed,
                seconds=30.0,
            )
            if process is None or created is None:
                w["script_problems"].append("the Direct server was never associated")
                return
            principal = (process.pid, created)
            r7_handles["original actor"] = process
        w["principal"] = list(principal)
        w["start_ticks"] = kernel_start_ticks(*principal)
        try:
            w["owner_group"] = os.getpgid(principal[0])
        except OSError:
            w["owner_group"] = None
        guardian = await run_owned(
            "find the guardian",
            wait_for_guardian,
            watcher.observed,
            principal[0],
            seconds=30.0,
        )
        if guardian is not None:
            w["guardian_group"] = guardian[1]
            opened = open_lifetime(watcher.observed(), guardian[0])
            if opened is not None:
                r7_handles["guardian"] = opened[0]
                w["guardian"] = [guardian[0], opened[1]]
        # The value stays in this frame; only the digest goes anywhere.
        marker = await run_owned(
            "read the launch marker",
            wait_for_marker,
            watcher.observed,
            principal,
            seconds=30.0,
        )
        w["marker_digest"] = marker.digest if marker is not None else None
        w["browser"] = list(marker.browser) if marker is not None else None
        lock = lock_identity(lock_path)
        w["lock"] = list(lock) if lock is not None else None
        await lease_checkpoint(
            BEFORE_CLOSE,
            holder=principal[0],
            alive={"original actor": True, "guardian": True},
        )
        # The trace, before anything is activated, on the original actor and
        # its guardian; both clocks sampled on either side of it.
        w["clocks"].append(clock_sample("before the trace"))
        traced = [principal[0]] + ([guardian[0]] if guardian is not None else [])
        if oracle.available:
            reason = await run_owned(
                "attach the trace", oracle.start, traced, seconds=60.0
            )
        else:
            reason = oracle.unavailable
        w["trace"] = {
            "attached": oracle.available and reason is None,
            "reason": reason,
            "pids": traced,
            "required": oracle.required,
        }
        w["traced_before_close"] = bool(w["trace"]["attached"])
        emit(
            "harness",
            "signal.oracle",
            phase="unconfirmed-close",
            attached=w["trace"]["attached"],
            reason=reason,
            ptrace_scope=oracle.scope,
            required=oracle.required,
            pids=traced,
        )
        if r7.activate:
            refused = []
            if fault_dir is None:
                refused.append("the row has no fault directory")
            if marker is None:
                refused.append("the launch marker was not read and matched")
            if w["start_ticks"] is None:
                refused.append("the original actor's kernel start is unknown")
            if oracle.required and not w["trace"]["attached"]:
                refused.append(f"the trace did not attach: {reason}")
            if refused:
                w["activation_refused"] = refused
            else:
                assert fault_dir is not None and marker is not None
                activation = publish_activation(
                    fault_dir,
                    row=row,
                    experiment=experiment,
                    repetition=r7.repetition,
                    run=log.run,
                    pid=principal[0],
                    start_ticks=w["start_ticks"],
                    role=role,
                    marker=marker.value,
                    source={
                        "python": command[0],
                        "revision": identity.get("head") or identity.get("pinned"),
                    },
                )
                # The event's own row, experiment and run are the row's.
                emit(
                    "harness",
                    "r7.activation",
                    **{
                        name: value
                        for name, value in activation.items()
                        if name not in ("row", "experiment", "run")
                    },
                )
        # Whatever starts after this marker started after the close began.
        w["close_ticks"], w["close_created"] = await run_owned(
            "mark the close", creation_marker, seconds=30.0
        )
        w["close_began"] = time.time()
        closed = await call("close_session", {})
        w["close"] = {
            name: closed.get(name)
            for name in (
                "began",
                "ended",
                "began_monotonic_ns",
                "ended_monotonic_ns",
                "is_error",
            )
        }
        w["clocks"].append(clock_sample("after the close"))
        emit("harness", "r7.window", phase="closed", **w["close"])
        if not daemon:
            # Direct keeps the profile until the host quits.
            await lease_checkpoint(
                AFTER_CONSUMPTION,
                holder=principal[0],
                alive={"original actor": True, "guardian": True},
            )
            w["recovery"] = R7_NO_RECOVERY
            await lease_checkpoint(
                BEFORE_QUIT,
                holder=principal[0],
                alive={"original actor": True, "guardian": True},
            )
            w["script_ended"] = True
            return
        if r7.control is not None:
            # A confirmed close releases the lease and the guardian, and the
            # owner keeps serving.
            await lease_checkpoint(
                AFTER_CONFIRMED_CLOSE,
                alive={"original actor": True, "guardian": False},
            )
            w["recovery"] = "none: a control's owner keeps serving"
            w["script_ended"] = True
            return
        # The owner gives way after an unconfirmed close. Nothing asks for the
        # profile until it and its guardian are seen gone and the lock is free
        # (E1EZ-02); an election may already have happened, a browser may not.
        w["owner_exit"] = await run_owned(
            "the original owner's exit",
            exit_state,
            r7_handles.get("original actor"),
            _R7_EXIT_SECONDS,
            seconds=_R7_EXIT_SECONDS + 30.0,
        )
        w["guardian_exit"] = await run_owned(
            "the original guardian's exit",
            exit_state,
            r7_handles.get("guardian"),
            _R7_EXIT_SECONDS,
            seconds=_R7_EXIT_SECONDS + 30.0,
        )
        point = await lease_checkpoint(BEFORE_RECOVERY)
        # The recovery barrier, kept: early use is judged against it again,
        # from the watcher's completed history, before the row is accepted.
        w["barrier_ticks"], w["barrier_created"] = await run_owned(
            "mark the barrier", creation_marker, seconds=30.0
        )
        pre = []
        if w["owner_exit"] != "exited":
            pre.append(f"the original owner was {w['owner_exit']!r}")
        if w["guardian_exit"] != "exited":
            pre.append(f"the original guardian was {w['guardian_exit']!r}")
        pre += checkpoint_problems(point, expect=lease_probe.FREE)
        pre += early_browsers(
            watcher.observed(),
            principal,
            since=w["close_created"],
            until=w["barrier_created"],
        )
        w["pre_probe"] = pre
        emit("harness", "r7.window", phase="barrier", pre_probe=pre)
        if pre:
            w["recovery"] = f"not made: {pre}"
            w["script_ended"] = True
            return
        gate("the recovery")
        probe = await call(READ_TOOL, READ_TOOL_ARGUMENTS)
        w["probe"] = {
            name: probe.get(name)
            for name in ("began", "ended", "is_error", "read_the_post")
        }
        w["recovery"] = R7_POST_SETTLEMENT
        # Before the host quits: a new owner, and only it, served the probe.
        served = not probe.get("is_error") and bool(probe.get("read_the_post"))
        found_problems: list[str] | None = None
        if served:
            deadline = time.monotonic() + _SUCCESSOR_SECONDS
            while True:
                found, found_problems = await run_owned(
                    "find the successor", find_the_successor_r7, probe, seconds=30.0
                )
                if found is not None:
                    r7_handles["successor"] = found
                if not found_problems or time.monotonic() >= deadline:
                    break
                await asyncio.sleep(0.2)
        verdict = successor_verdict(
            probe=probe,
            left=w["owner_exit"] == "exited",
            problems=found_problems,
        )
        w["successor_problems"] = verdict
        w["successor_verified"] = not verdict
        emit("harness", "owner.successor", problems=verdict, verified=not verdict)
        gate("the host's quit")
        w["script_ended"] = True

    async def r7_step(label: str, func: Callable[..., Any], *args: Any, **kw: Any):
        """One step of H-R7's settling: owned, bounded, never gated, and with
        every cancellation held for the whole teardown (``r7_defer``). A step
        that fails is recorded and answers None; the next one still runs."""
        seconds = kw.pop("seconds")
        try:
            return await run_owned(
                label, func, *args, seconds=seconds, gated=False, defer=r7_defer, **kw
            )
        except Exception as exc:  # noqa: BLE001 - recorded, the next step runs
            r7_window["script_problems"].append(
                f"settling: {label} failed: {type(exc).__name__}: {exc}"
            )
            return None

    async def r7_after_quit() -> None:
        """Settle what the scenario leaves running, after every measurement.

        Direct: the host's quit is the scenario's own end, so the server's
        and its guardian's exits are observed, not caused. Daemon: nothing
        idles out, so each owner still serving is ended through the handle
        the row identified it by, labelled as the harness's, and its guardian
        seen gone; none of that is read as the product settling.

        Run once, after the host's quit or, when the row was cancelled or
        failed before it, from the teardown; either way the serving owner is
        the one the row found serving, else the one the descriptor names now
        as ``identify_owner`` ties it to this row, so a successor elected
        after the last look is not left running. Each step runs whatever the
        one before it did (``r7_step``). Anything not shown gone is retained
        (``retain``): no later measurement starts until it is.
        """
        nonlocal cleanup_owner, r7_settling_began
        r7_settling_began = True
        w = r7_window
        if not daemon:
            server_handle = r7_handles.get("original actor")
            guardian_handle = r7_handles.get("guardian")
            w["server_exit"] = await r7_step(
                "the server's exit", exit_state, server_handle, 30.0, seconds=60.0
            )
            w["guardian_after_quit"] = await r7_step(
                "the guardian's exit",
                exit_state,
                guardian_handle,
                _R7_EXIT_SECONDS,
                seconds=_R7_EXIT_SECONDS + 30.0,
            )
            for label, handle, state in (
                ("the Direct server", server_handle, w["server_exit"]),
                ("the Direct guardian", guardian_handle, w["guardian_after_quit"]),
            ):
                if handle is not None and state != "exited":
                    retain(
                        f"{label} {handle.pid}",
                        lambda grace, handle=handle: (
                            exit_state(handle, grace) == "exited"
                        ),
                    )
            return
        serving = r7_handles.get("successor")
        # Only looked at, never read into being (``find_the_owner``).
        if (
            serving is None
            and daemon_descriptor.descriptor_path(account.auth_root).exists()
        ):
            found = await r7_step(
                "find the serving owner", find_the_successor_r7, {}, seconds=30.0
            )
            serving = found[0] if found else None
        cleanup_owner = serving or identified
        seen: set[tuple[int, float]] = set()
        for who, running in (
            ("serving owner", serving),
            ("original owner", identified),
        ):
            if running is None or (running.pid, running.create_time) in seen:
                continue
            seen.add((running.pid, running.create_time))
            ended = (
                await r7_step(f"end the {who}", end_owner, running, seconds=60.0)
                or "unknown: the end was not answered"
            )
            if ended not in ("gone", "stopped"):
                retain(
                    f"the {who} {running.pid}",
                    lambda grace, running=running: (
                        end_owner(running) in ("gone", "stopped")
                    ),
                )
            observed = watcher.observed()
            guardian = guardian_launch(observed, running.pid)
            guardian_exit = "none was seen"
            if guardian is not None:
                guardian_exit = (
                    await r7_step(
                        f"the {who}'s guardian's exit",
                        lifetime_exit_state,
                        observed,
                        guardian[0],
                        _R7_EXIT_SECONDS,
                        seconds=_R7_EXIT_SECONDS + 30.0,
                    )
                    or "unknown: the wait was not answered"
                )
                if guardian_exit != "exited":
                    retain(
                        f"the {who}'s guardian {guardian[0]}",
                        lambda grace, pid=guardian[0], records=observed: (
                            lifetime_exit_state(records, pid, grace) == "exited"
                        ),
                    )
            record = {
                "who": who,
                "pid": running.pid,
                "start_identity": running.create_time,
                "result": ended,
                "guardian": guardian[0] if guardian else None,
                "guardian_exit": guardian_exit,
            }
            w["ended_by_harness"].append(record)
            emit("harness", "r7.ended", **record)
        if identified is not None:
            original = next(
                (
                    record
                    for record in w["ended_by_harness"]
                    if (record["pid"], record["start_identity"])
                    == (identified.pid, identified.create_time)
                ),
                None,
            )
            by_itself = w.get("owner_exit") == "exited"
            gone = by_itself or (
                original is not None and original["result"] in ("gone", "stopped")
            )
            # Gone either way is what cleanup and the preservation gate ask;
            # whether it went by itself is the continuation's, not this record's.
            owner["exit"] = {
                "how": "exited" if gone else (original or {}).get("result"),
                "by": "itself" if by_itself else "the harness, after measurement",
            }

    async def after_call() -> None:
        await find_the_owner()
        if kill_actor:
            await kill_the_actor()

    after: ProfileSnapshot | None = None
    actors_ended: float | None = None
    residual: list[int] = []
    teardown: list[str] = []
    r7_phase: PhaseReading | None = None
    r7_shared = SharedReduction()
    #: What ended the row's try, if anything: the teardown raises a
    #: cancellation it held only when that is not already one.
    row_failure: BaseException | None = None
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
            script=(
                job_query_script
                if shim is not None
                else r7_script
                if r7 is not None
                else None
            ),
        )
        result.host = host
        if r7 is not None:
            try:
                await r7_after_quit()
            except Exception as exc:  # noqa: BLE001 - the row's evidence; the teardown goes on
                r7_window["script_problems"].append(
                    f"settling after the quit failed: {type(exc).__name__}: {exc}"
                )
            # Every owner settled, the cancellation held meanwhile ends the
            # row; the teardown below still runs whole.
            held = r7_defer.take()
            if held is not None:
                raise held
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

        if identified is not None and r7 is not None:
            # H-R7 settled its owners in ``r7_after_quit``: none idles out.
            log_path = Path(owner.get("log_path") or "")
            if log_path.is_file():
                lines = log_path.read_text(errors="replace").splitlines()
                owner["log_tail"] = lines[-200:]
                for line in owner["log_tail"]:
                    emit("owner", "user.output", stream="owner-log", line=line)
            emit("harness", "owner.exit", **(owner.get("exit") or {}))
        elif identified is not None:
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
            # Ended with their Jobs when the server or owner went (Direct's
            # only boundary). The wait is bounded and settles nothing by
            # itself: what counts is the verdict taken here, before the
            # teardown touches the cache or stops the stall host, and nothing
            # after it can improve that verdict.
            watch_late_installers()
            await asyncio.to_thread(fates.settle, _BROWSER_GONE_SECONDS)
            family_at_teardown = await asyncio.to_thread(
                family_problems,
                watcher.observed(),
                fates,
                reached(shim.reached_file),
            )
        residual = await asyncio.to_thread(
            wait_for_no_browser, account, _BROWSER_GONE_SECONDS
        )
        actors_ended = time.time()
        after = snapshot(account.profile, expected_digest=staged.li_at_digest)
        result.after = after
        emit("harness", "profile.snapshot", phase="after", **after.as_event_fields())
    except BaseException as exc:
        row_failure = exc
        raise
    finally:
        if actors_ended is None:
            actors_ended = time.time()
        if r7 is not None and not r7_settling_began:
            # Cancelled or failed before the host's quit was settled: the
            # same settling, in this teardown, with its cancellations held.
            try:
                await r7_after_quit()
            except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
                teardown.append(f"the H-R7 actors could not be settled: {exc!r}")
        # A failed row's cleanup differs from restoration for reuse: while the
        # family is not shown ended, a download may still be running, and it
        # is evidence, not litter.
        unresolved_family = cache is not None and family_at_teardown != []
        if cache is not None and not unresolved_family:
            # Restoration for reuse: the row-private cache goes, and the real
            # cache is recorded again for the post-quit session.
            try:
                cache.dismantle()
            except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
                teardown.append(f"the private browser cache stayed: {exc!r}")
            try:
                await asyncio.to_thread(
                    record_install,
                    runtime.python,
                    {**env, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)},
                )
            except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
                teardown.append(f"the real cache's install was not recorded: {exc!r}")
        elif cache is not None:
            # The private cache, its held-back place and the install records
            # stay exactly as the row left them, and so does what is known of
            # each installer, read before the harness intervenes below.
            emit(
                "harness",
                "job_query.window",
                phase="failed-row cleanup",
                unresolved=family_at_teardown,
                private_cache=str(cache.directory),
                held=str(cache.held) if cache.held is not None else None,
                fates=[fate.as_event_fields() for fate in fates.fates.values()],
            )
            teardown.append(
                f"the installer family was not shown ended, so the private cache "
                f"{cache.directory} (held back: {cache.held}) and the install "
                f"records were left as they were"
            )
        if stall is not None:
            stall_url = stall.url
            stall.stop()
            if unresolved_family:
                # Do not credit later exits as pre-intervention settlement.
                teardown.append(
                    f"the harness stopped its stall host {stall_url} with the "
                    f"installer family unresolved; a later exit cannot establish "
                    f"product settlement before this intervention; its cause "
                    f"remains unobserved"
                )
        # Each helper is ended whatever the one before it did; a failure is
        # the row's to report.
        confirmed = [killed["pid"]] if killed.get("exit") == "killed" else []
        try:
            # Once its tracees have exited, strace has seen every signal they
            # sent. H-R7 owns the wait and holds a cancellation for the whole
            # teardown, so the watcher and the canaries below still stop.
            if r7 is not None:
                outcome = await run_owned(
                    "stop the trace",
                    oracle.stop,
                    confirmed_dead=confirmed,
                    seconds=180.0,
                    gated=False,
                    defer=r7_defer,
                )
            else:
                outcome = await asyncio.to_thread(oracle.stop, confirmed_dead=confirmed)
        except Exception as exc:  # noqa: BLE001 - reported, the teardown goes on
            teardown.append(f"the signal oracle could not be stopped: {exc!r}")
            outcome = OracleOutcome(
                status=O2_INCOMPLETE,
                required=oracle.required,
                reasons=[f"the oracle could not be stopped: {exc!r}"],
            )
        if r7 is not None:
            # ``stop`` returning is not the tracer gone: its last wait is
            # bounded. Asked of the tracer's own process, and kept if not.
            try:
                traced_out = oracle.settled()
            except Exception as exc:  # noqa: BLE001 - an unanswered question settles nothing
                teardown.append(f"the trace's end could not be asked about: {exc!r}")
                traced_out = False
            if not traced_out:
                retain("the row's trace", lambda grace: oracle.end())
                teardown.append("the trace is not shown ended; it stays retained")
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
        if r7 is not None:
            # The whole transcript first, then the phase: after the real drain
            # returned, on strace's clock, from the fault's monotonic return.
            r7_window["clocks"].append(clock_sample("after the trace"))
            try:
                text = oracle.out.read_text(errors="replace")
            except OSError:
                text = ""
            returned = published_return(fault_dir) if activation is not None else None
            boundary = (
                realtime_interval(returned, r7_window["clocks"])
                if returned is not None
                else "no real drain return was published"
            )
            principal_pid = (r7_window.get("principal") or [None])[0]
            r7_phase = read_phase(
                outcome,
                text,
                owner=principal_pid,
                guardian=(r7_window.get("guardian") or [None])[0],
                owner_group=r7_window.get("owner_group"),
                boundary=boundary,
                history=ProcessHistory(observed_events, outside=[os.getpid()]),
                marker=r7_window.get("marker_digest"),
            )
            r7_shared = shared_reduction(o2.resolved, len(o2.unknowns), r7_phase)
            emit(
                "harness",
                "r7.phase",
                collection=r7_phase.collection,
                reasons=list(r7_phase.reasons),
                boundary=list(r7_phase.boundary) if r7_phase.boundary else None,
                clock=r7_phase.clock,
                calls=len(r7_phase.calls),
                tracees=r7_phase.tracees,
            )
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
                # The declared shim venv or fault overlay is where the actors
                # start from; that it imports the runtime's code was checked
                # when it was made.
                replace(runtime, python=shim.python)
                if shim
                else replace(runtime, python=overlay.python)
                if overlay
                else runtime,
                candidate_prefix=sys.prefix,
                owner_expected=bool(owner.get("pid")),
            )
        row_requests = list(origin.requests[request_mark:])
        row_decisions = list(proxy.decisions[decision_mark:])
        result.cleanup = retire_daemon_state(
            account, cleanup_owner if cleanup_owner is not None else identified
        )
        if r7 is not None and daemon:
            # Whatever the root still publishes is an owner this row
            # identified, and settled or retained, or it is held: cleanup
            # left it running unsignalled.
            known = {
                (owner_identity.pid, owner_identity.instance_id)
                for owner_identity in (
                    identified,
                    cleanup_owner,
                    r7_handles.get("successor"),
                )
                if owner_identity is not None
            }
            try:
                if daemon_descriptor.descriptor_path(account.auth_root).exists():
                    left_published = daemon_descriptor.read(account.auth_root)
                    if (
                        left_published is not None
                        and (left_published.pid, left_published.instance_id)
                        not in known
                    ):
                        hold_publication(
                            left_published.pid,
                            "still published after cleanup, and not an owner this "
                            "row identified",
                        )
            except Exception as exc:  # noqa: BLE001 - unread is unknown, and held
                hold_publication(None, f"after cleanup: {type(exc).__name__}: {exc}")
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
        if r7 is not None:
            # The teardown is done: a cancellation it held is raised now, and
            # nothing is preserved or measured after it. A cancellation that
            # ended the try is already on its way; a later one is not lost
            # behind another failure.
            held = r7_defer.take()
            if held is not None and not isinstance(row_failure, asyncio.CancelledError):
                if row_failure is not None:
                    held.add_note(
                        f"held while the row's teardown ran after {row_failure!r}"
                    )
                raise held

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
    # Every actor that could plant a failure has exited by now; a record
    # still missing is simply not a witness.
    records = reached(shim.reached_file) if shim is not None else []
    observation = job_query_problems(
        shim,
        fates=fates,
        window=job_window,
        script_error=host.script_error,
        observed=observed_events,
        records=records,
        host=host_failures(host),
        watcher=watcher_failures(
            result.watcher,
            actors_began=actors_began,
            actors_ended=actors_ended,
            browser_key=account.browser_key,
        ),
        cleanup=list(result.cleanup.failures) if result.cleanup else [],
        before_cleanup=family_at_teardown,
    )
    # The census, cleanup and owner checks, before H-R11 adds its own: the
    # continuation's validity carries these, and them once.
    settled_refusals = list(refusals)
    if shim is not None:
        # An installer whose end was not observed may still be running on the
        # profile's setup, so no session starts after the row until every one
        # of them is an observed exit, and every lifetime the drain asked
        # about is one of them.
        refusals += [f"H-R11 evidence incomplete: {p}" for p in observation]
    if r7 is not None:
        # Before preservation: the original actors, and whatever the harness
        # ended, positively gone, the lock free, nothing of the row's own
        # still running or unsettled. Any of that missing launches nothing.
        r7_refusals = r7_settled_problems(r7_window, daemon=daemon)
        try:
            point = await lease_checkpoint(BEFORE_PRESERVATION)
            r7_refusals += checkpoint_problems(point, expect=lease_probe.FREE)
        except Exception as exc:  # noqa: BLE001 - a refusal, and the row's evidence
            r7_refusals.append(f"the lock could not be asked about: {exc}")
        r7_refusals += settlement_problems()
        r7_window["before_preservation"] = r7_refusals
        refusals += [f"H-R7: {p}" for p in r7_refusals]
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
    if shim is not None:
        for fate in fates.fates.values():
            emit("harness", "installer.fate", **fate.as_event_fields())
        if fates.alive():
            observation.append(
                f"installers outlived the row: {[f.pid for f in fates.alive()]}"
            )
        result.failures += observation
        # A known-bad control keeps its behaviour, never a failure to observe,
        # to settle or to clean up after it: every experiment is held to these.
        result.continuation = native_continuation(
            experiment=experiment,
            run=log.run,
            daemon=daemon,
            identity=identity,
            shim=shim,
            vector=result.vector,
            host=host,
            window=job_window,
            fates=fates,
            observed=observed_events,
            records=records,
            events=logged(shim.reached_file),
            successor=successor,
            validity=[
                *observation,
                *settled_refusals,
                *result.runtime_failures,
                *(f"canary placement: {problem}" for problem in canary_problems),
                *(f"teardown: {problem}" for problem in teardown),
            ],
        )
        emit(
            "harness",
            "shim.reached",
            lines=records,
            events=logged(shim.reached_file),
            witnesses=list(result.continuation.witnesses),
            shim_sha256=shim.shim_sha256,
        )
        emit(
            "harness",
            "job_query.continuation",
            # The event's own experiment and run are the row's; the vector is
            # in row.outcome and the witnesses in shim.reached.
            **{
                name: value
                for name, value in asdict(result.continuation).items()
                if name not in ("experiment", "run", "vector", "witnesses")
            },
        )
    if r7 is not None:
        result.unconfirmed = r7_continuation(
            r7,
            window=r7_window,
            experiment=experiment,
            run=log.run,
            daemon=daemon,
            identity=identity,
            fault_dir=fault_dir,
            activation=activation,
            runtime=runtime,
            env=env,
            host=host,
            vector=result.vector,
            phase=r7_phase,
            observed=observed_events,
            shared=r7_shared,
            validity=[
                *host_failures(host),
                *(
                    [f"the H-R7 script failed: {host.script_error}"]
                    if host.script_error
                    else []
                ),
                *r7_window["script_problems"],
                *(
                    []
                    if r7_window.get("script_ended") is True
                    else ["the H-R7 script did not run to its end"]
                ),
                *(
                    f"activation refused: {p}"
                    for p in r7_window.get("activation_refused") or []
                ),
                *(
                    f"watcher: {problem}"
                    for problem in watcher_failures(
                        result.watcher,
                        actors_began=actors_began,
                        actors_ended=actors_ended,
                        browser_key=account.browser_key,
                    )
                ),
                *(f"cleanup: {p}" for p in result.cleanup.failures),
                *settled_refusals,
                *(
                    f"before preservation: {p}"
                    for p in r7_window["before_preservation"]
                ),
                *result.runtime_failures,
                *(f"canary placement: {problem}" for problem in canary_problems),
                *(f"teardown: {problem}" for problem in teardown),
            ],
        )
        (work_dir / "r7.json").write_text(
            json.dumps(
                {**r7_window, "clocks": [asdict(s) for s in r7_window["clocks"]]},
                indent=2,
                default=str,
            )
            + "\n"
        )
        emit(
            "harness",
            "r7.continuation",
            **{
                name: value
                for name, value in asdict(result.unconfirmed).items()
                if name not in ("experiment", "run", "vector", "phase")
            },
        )
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
                "continuation": (
                    asdict(result.continuation)
                    if result.continuation is not None
                    else None
                ),
                "unconfirmed": (
                    asdict(result.unconfirmed)
                    if result.unconfirmed is not None
                    else None
                ),
            },
            indent=2,
            default=str,
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
