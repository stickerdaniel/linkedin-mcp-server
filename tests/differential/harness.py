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
leaves, closes stdin and then escalates to signals after two seconds. A host
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
with the daemon off. The frozen-baseline K1 and K2 of the plan are a later
stage, and nothing here claims them.
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
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
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
from fastmcp.client.transports.base import ClientTransport, SessionKwargs
from mcp import ClientSession
from mcp.shared.message import SessionMessage
from typing_extensions import Unpack

from differential.events import EventLog, read_jsonl
from differential.session import (
    RETAINED,
    UNCERTAIN,
    ProfileSnapshot,
    r17_outcome,
    snapshot,
    stage_signed_in_session,
)
from differential.synthetic_origin import (
    POST_MARKER,
    EgressProxy,
    SyntheticOrigin,
)
from differential.watcher import USER_DATA_DIR_FLAG, canonical_user_data_dir
from linkedin_mcp_server import daemon_descriptor
from linkedin_mcp_server.config.loaders import EnvironmentKeys

REAL_AUTH_ROOT_NAME = ".linkedin-mcp"

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "mcp-server-linkedin"

#: The browser cache this run was started with. Read at import, because the
#: suite's ``reset_bootstrap_for_testing`` deletes the variable per test.
_INHERITED_BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")

WATCHER_SCRIPT = Path(__file__).with_name("watcher.py")

ROW_H_R1 = "H-R1"
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

#: The largest wall-clock gap between two watcher samples a row accepts. O1 is
#: a sampled claim, and this is its resolution: a second browser on the profile
#: is a whole Chromium launch, which has to live through its own startup, well
#: over a second on these runners, before it can exit again, so a gap under a
#: second cannot hide one that got that far. Twenty times the target interval,
#: which leaves room for a busy runner without letting a stalled watcher pass.
#: The same bound limits how long a row actor may stay alive and unreadable
#: before the census counts as uncertain, for the same reason.
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
_OWNER_MODULE = "linkedin_mcp_server.daemon_owner"


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


def claim_account(profile: Path) -> ActorAccount:
    """Refuse the user's own auth root, before anything touches the profile.

    Refused in both directions: an auth root at or inside ``~/.linkedin-mcp``,
    and one at or above it, such as the home directory itself.

    First as written, as strings, so a profile named inside the real root is
    refused without a single filesystem call beneath it. Then by the
    filesystem's identity (device and inode) of the auth root and each of its
    ancestors against the real root and each of its ancestors. Identity is what
    the filesystem itself says is the same directory, so a case alias on a
    case-insensitive volume and a link are both caught, and two names a
    case-sensitive volume keeps apart stay apart. The auth root has to exist:
    an identity that cannot be read cannot be judged.
    """
    reals = [
        os.path.join(os.path.abspath(home), REAL_AUTH_ROOT_NAME)
        for home in account_homes()
    ]
    raw = os.path.abspath(os.path.expanduser(profile))
    auth_root = os.path.dirname(raw)
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
    return ActorAccount(Path(os.path.realpath(raw)))


def actor_environment(
    account: ActorAccount, proxy_url: str, *, daemon: bool, browsers: Path
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
        if key not in settings and not key.startswith("LINKEDIN")
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
    return env


def server_command() -> list[str]:
    return [sys.executable, "-m", "linkedin_mcp_server"]


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


# --- Watcher -----------------------------------------------------------------


class Watcher:
    """The watcher process, and the events it wrote once it has stopped."""

    def __init__(
        self, directory: Path, log: EventLog, *, experiment: str, row: str
    ) -> None:
        directory.mkdir(parents=True, exist_ok=True)
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
            "--unreadable-bound",
            str(MAX_WATCHER_GAP_SECONDS),
        ]
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
        # never reported as started. Waiting for it keeps the actors out of it.
        deadline = time.monotonic() + ready_seconds
        while time.monotonic() < deadline:
            if any(r.get("kind") == "watcher.ready" for r in read_jsonl(self.out)):
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError(
            f"the watcher did not take its baseline sample: "
            f"{self.stderr.read_text(errors='replace')[-2000:]}"
        )

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
    # Only a read failure that outlived the bound: a shorter one ended before a
    # hidden browser could have got past its own startup. Every failure stays
    # in the summary's ``read_failures`` either way.
    unread = summary.get("relevant_read_failures") or []
    if unread:
        failures.append(
            f"the watcher could not read {len(unread)} row actors' metadata for "
            f"longer than {summary.get('unreadable_bound_seconds')}s: {unread[:5]}"
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
        self, **session_kwargs: Unpack[SessionKwargs]
    ) -> AsyncIterator[ClientSession]:
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
                            try:
                                message = mcp_types.JSONRPCMessage.model_validate_json(
                                    line
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
                            by_alias=True, exclude_none=True
                        )
                        await process.stdin.send((data + "\n").encode())

        async with anyio.create_task_group() as tasks:
            tasks.start_soon(pump_stdout)
            tasks.start_soon(pump_stdin)
            tasks.start_soon(self._pump_stderr, process)
            try:
                async with ClientSession(
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
    structured = result.structuredContent or {}
    if isinstance(structured.get("result"), dict):
        structured = structured["result"]
    sections = structured.get("sections")
    feed = sections.get("feed") if isinstance(sections, dict) else None
    return {
        "is_error": bool(result.isError),
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


async def run_host_session(
    command: Sequence[str],
    *,
    env: dict[str, str],
    cwd: Path,
    on_stderr: Callable[[str], None],
    after_call: Callable[[], Awaitable[None]] | None = None,
    tool: str = READ_TOOL,
    arguments: dict[str, Any] | None = None,
) -> HostSession:
    """Initialize, call the read tool once, then quit the way a host does."""
    session = HostSession()

    def remember(line: str) -> None:
        session.stderr.append(line)
        session.user_lines.append(line)
        on_stderr(line)

    transport = HostQuitTransport(command, env=env, cwd=cwd, on_stderr=remember)
    client = Client(transport, init_timeout=_INIT_SECONDS)
    try:
        async with client:
            result = await client.call_tool_mcp(
                tool,
                READ_TOOL_ARGUMENTS if arguments is None else arguments,
                timeout=_CALL_SECONDS,
            )
            session.tool = tool_summary(result)
            session.user_lines += session.tool["text"].splitlines()
            if after_call is not None:
                await after_call()
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


def _profile_processes(account: ActorAccount) -> list[psutil.Process]:
    """Every process, root or child, running a browser on this row's profile."""
    found = []
    for process in psutil.process_iter(["cmdline"]):
        cmdline = process.info.get("cmdline") or ()
        for argument in cmdline:
            if not argument.startswith(USER_DATA_DIR_FLAG):
                continue
            value = argument[len(USER_DATA_DIR_FLAG) :]
            if canonical_user_data_dir(value) == account.browser_key:
                found.append(process)
            break
    return found


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


@dataclass(frozen=True)
class OwnerDisposition:
    #: The owner this row identified is provably not running, or none was.
    gone: bool
    #: Cleanup sent the owner a signal.
    signalled: bool
    failures: tuple[str, ...] = ()


def identify_owner(
    published: Any,
    account: ActorAccount,
    *,
    open_process: Callable[[int], Any] = psutil.Process,
) -> tuple[OwnerIdentity | None, str | None]:
    """Bind the descriptor's owner to a live process, or say why it cannot be."""
    try:
        if canonical_user_data_dir(published.profile_path) != account.browser_key:
            return None, "the descriptor serves another profile"
    except (AttributeError, TypeError):
        return None, "the descriptor names no profile"
    try:
        process = open_process(published.pid)
        created = process.create_time()
        cmdline = " ".join(process.cmdline())
    except psutil.Error as exc:
        return None, f"pid {published.pid} could not be read ({type(exc).__name__})"
    if _OWNER_MODULE not in cmdline:
        return None, f"pid {published.pid} is not a daemon owner"
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

    Nothing is ever looked up by pid here. The only process that may receive a
    signal is ``owner.process``, the handle taken when the row identified it,
    and psutil refuses to signal through it once its pid names a process with
    another create time. Everything else is refused and reported.
    """
    if read_error is not None:
        return OwnerDisposition(
            False, False, (f"the row's descriptor could not be read: {read_error}",)
        )
    if owner is None:
        if published is None:
            return OwnerDisposition(True, False)
        return OwnerDisposition(
            False,
            False,
            (
                f"the descriptor names pid {published.pid}, instance "
                f"{published.instance_id}, which this row never identified; "
                f"not signalled",
            ),
        )
    if owner.auth_root != auth_root:
        return OwnerDisposition(
            False,
            False,
            (f"the identified owner belongs to {owner.auth_root}; not signalled",),
        )
    if published is not None and published.instance_id != owner.instance_id:
        return OwnerDisposition(
            False,
            False,
            (
                f"the descriptor names instance {published.instance_id}, the row "
                f"identified {owner.instance_id}; not signalled",
            ),
        )
    try:
        running = owner.process.is_running()
    except psutil.Error:
        running = False
    if not running:
        return OwnerDisposition(True, False)
    try:
        owner.process.kill()
    except psutil.NoSuchProcess:
        return OwnerDisposition(True, False)
    except psutil.Error as exc:
        return OwnerDisposition(
            False, False, (f"the owner could not be stopped: {type(exc).__name__}",)
        )
    try:
        owner.process.wait(timeout=wait_seconds)
    except psutil.TimeoutExpired:
        return OwnerDisposition(
            False,
            True,
            (f"the owner was still running {wait_seconds}s after it was killed",),
        )
    except psutil.NoSuchProcess:
        pass
    return OwnerDisposition(True, True)


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


def row_expectations(vector: RowVector) -> list[str]:
    """What H-R1 requires of either mode; each unmet one is a failure."""
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
    if vector.mode == "daemon":
        if not vector.owner_published:
            failures.append("daemon mode published no owner")
        if vector.fell_back:
            failures.append("daemon mode fell back to a Direct server")
    elif vector.owner_published:
        failures.append("the Direct reference reached a shared owner")
    return failures


def compare_repeat(first: RowVector, second: RowVector) -> list[str]:
    """K0: every field of two runs of the same experiment must agree."""
    one, two = asdict(first), asdict(second)
    return [
        f"{name}: {one[name]!r} then {two[name]!r}"
        for name in one
        if one[name] != two[name]
    ]


def compare_to_direct(direct: RowVector, daemon: RowVector) -> list[str]:
    """K3 against the same-revision Direct reference: O1 and O4 must be ``=``."""
    differences = []
    for name in ("o1_single_browser", "o4_session"):
        if getattr(direct, name) != getattr(daemon, name):
            differences.append(
                f"{name}: Direct {getattr(direct, name)!r}, daemon "
                f"{getattr(daemon, name)!r}"
            )
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


@dataclass
class RowResult:
    experiment: str
    mode: str
    vector: RowVector | None = None
    before: ProfileSnapshot | None = None
    after: ProfileSnapshot | None = None
    host: HostSession | None = None
    owner: dict[str, Any] | None = None
    watcher: dict[str, Any] | None = None
    cleanup: DaemonCleanup | None = None
    post_quit: PostQuit | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.experiment} ({DIRECT_REFERENCE if self.mode == 'direct' else self.mode})"

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

    host_problems = host_failures(host)
    failures += host_problems

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
    if observed.daemon:
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
        owner_published = bool(owner.get("descriptor_present")) or forwarded

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
        host_exit_clean=not host_problems,
        cleanup_clean=cleanup_clean,
    )
    return vector, row_expectations(vector) + failures


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


async def observe_preservation(
    account: ActorAccount,
    origin: SyntheticOrigin,
    proxy: EgressProxy,
    *,
    command: Sequence[str],
    browsers: Path,
    work_dir: Path,
    on_stderr: Callable[[str], None],
) -> PostQuit:
    """Start one Direct host on the profile and ask the origin about its session.

    After the row's interval, with its actors gone, through the same proxy and
    fence. Nothing is re-staged first: that would repair the loss this exists
    to see. Its own browser has to be gone before the row is judged.
    """
    mark = len(origin.requests)
    session = await run_host_session(
        command,
        env=actor_environment(account, proxy.url, daemon=False, browsers=browsers),
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
) -> RowResult:
    """Run H-R1 once and return its outcome vector and evidence."""
    # First, before anything reads, launches or spawns.
    account = claim_account(profile)

    origin, proxy = egress
    row = ROW_H_R1
    mode = "daemon" if daemon else "direct"
    result = RowResult(experiment=experiment, mode=mode)
    command = list(command or server_command())

    def emit(actor: str, kind: str, **fields: Any) -> None:
        log.emit(experiment=experiment, row=row, actor=actor, kind=kind, **fields)

    identity = row_identity()
    refusal = evidence_refusal(identity, ci=bool(os.environ.get("CI")))
    if refusal is not None:
        raise EvidenceRefused(refusal)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "identity.json").write_text(json.dumps(identity, indent=2) + "\n")
    emit("harness", "row.identity", mode=mode, **identity)

    staged = await stage_signed_in_session(
        account.profile, accept=lambda session: origin.accept_session(session.li_at)
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
    browsers = Path(
        os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default_browsers_path()
    )
    env = actor_environment(account, proxy.url, daemon=daemon, browsers=browsers)
    watcher = Watcher(work_dir, log, experiment=experiment, row=row)
    watcher.start()
    actors_began = time.time()

    owner: dict[str, Any] = {}
    identified: OwnerIdentity | None = None

    async def find_the_owner() -> None:
        nonlocal identified
        if not daemon:
            # A Direct server publishes nothing; a descriptor here would be one.
            owner["descriptor_present"] = daemon_descriptor.descriptor_path(
                account.auth_root
            ).exists()
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
        identified, problem = identify_owner(published, account)
        if identified is None:
            owner["identify_error"] = problem
        else:
            owner["start_identity"] = identified.create_time
        emit("harness", "owner.found", **owner)

    after: ProfileSnapshot | None = None
    actors_ended: float | None = None
    residual: list[int] = []
    try:
        host = await run_host_session(
            command,
            env=env,
            cwd=work_dir,
            on_stderr=lambda line: emit(
                "frontend", "user.output", stream="stderr", line=line
            ),
            after_call=find_the_owner,
        )
        result.host = host
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
        # The row's interval ends here: the watcher stops before anything else
        # starts on the profile.
        result.watcher = watcher.stop()
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
    post_quit: PostQuit | None = None
    if result.cleanup.owner_gone:
        post_quit = await observe_preservation(
            account,
            origin,
            proxy,
            command=command,
            browsers=browsers,
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
        )
    )
    (work_dir / "failures.json").write_text(
        json.dumps(
            {
                "label": result.label,
                "vector": asdict(result.vector),
                "failures": result.failures,
            },
            indent=2,
        )
        + "\n"
    )
    emit(
        "harness",
        "row.outcome",
        mode=mode,
        reference=DIRECT_REFERENCE if not daemon else None,
        vector=asdict(result.vector),
        failures=result.failures,
        cleanup=asdict(result.cleanup),
    )
    return result
