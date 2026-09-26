"""The harness core: account boundary, watcher, host stub, and row H-R1.

**Account boundary.** Spawned actors use the account's *real* daemon state
root, because ``daemon_descriptor._account_home`` ignores ``HOME`` on purpose
and the owner is started by production code that has nowhere to inject a
redirection. What keeps them off the user's state is the key: the daemon
directory is a hash of the auth root, and every row's auth root is a fresh
temporary directory, so cleanup has one exact target (``daemon_dir``) and stops
a published owner by the pid its own descriptor names. This is the policy of
``real_state_root`` in ``tests/test_daemon_election.py``.

The one auth root no row may ever use is the user's, ``~/.linkedin-mcp``.
``claim_account`` refuses it, and anything that contains it or sits inside it,
before a file is read, a browser launched or a process spawned. Every entry
point here goes through it first; the containment sentinel proves that order.
``HOME`` is deliberately not redirected for the actors: on Linux the bundled
browser reads the trusted test CA from ``~/.pki/nssdb``, so a different home
would be a different trust store and the synthetic origin would stop loading.

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
host, and wait for everything the row started to be gone. In daemon mode that
includes the owner, which leaves through its own idle exit. Before and after,
the R17 snapshot. The outcome vector is what K0 compares across repeats and K3
compares against K1.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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

#: The browser cache this run was started with. Read at import, because the
#: suite's ``reset_bootstrap_for_testing`` deletes the variable per test.
_INHERITED_BROWSERS_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")

WATCHER_SCRIPT = Path(__file__).with_name("watcher.py")

ROW_H_R1 = "H-R1"
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

_HOST_EXIT_SECONDS = 90.0
_OWNER_EXIT_SLACK_SECONDS = 90.0
_BROWSER_GONE_SECONDS = 60.0
_INIT_SECONDS = 180.0
_CALL_SECONDS = 240.0
_STDERR_EOF_SECONDS = 10.0

_FORWARDING_LINE = "Forwarding to the shared browser owner"
_IDLE_EXIT_LINE = "Nothing has needed the browser in"


class ContainmentError(RuntimeError):
    """The configured auth root is the user's own, or would reach it."""


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


def real_auth_roots() -> tuple[str, ...]:
    """Every spelling of the user's own auth root this process can name.

    Both the ``HOME`` home and the account's own, since the two can differ and
    the daemon keys on the second. ``realpath`` follows links along the path
    and lists nothing inside it.
    """
    homes = [Path.home()]
    with contextlib.suppress(Exception):
        homes.append(daemon_descriptor._account_home())
    spellings: set[str] = set()
    for home in homes:
        root = os.path.join(os.path.abspath(home), REAL_AUTH_ROOT_NAME)
        spellings.add(os.path.normcase(root))
        spellings.add(os.path.normcase(os.path.realpath(root)))
    return tuple(sorted(spellings))


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        # Different drives on Windows: neither contains the other.
        return False


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


def _refuse_overlap(profile: Path, spelling: str) -> None:
    auth_root = os.path.dirname(spelling)
    for real in real_auth_roots():
        if _within(auth_root, real) or _within(real, auth_root):
            raise ContainmentError(
                f"refusing profile {profile}: its auth root {auth_root} overlaps "
                f"the account's own {real}. A row uses a temporary auth root and "
                f"nothing else."
            )


def claim_account(profile: Path) -> ActorAccount:
    """Refuse the user's own auth root, before anything touches the profile.

    Refused in both directions: an auth root inside ``~/.linkedin-mcp``, and an
    auth root that contains it, such as the home directory itself. The path as
    written is judged first, as a string, so a profile named inside the real
    root is refused without a single filesystem call beneath it. Only a path
    that passes is resolved and judged again, which catches a link into it.
    """
    raw = os.path.abspath(os.path.expanduser(profile))
    _refuse_overlap(profile, os.path.normcase(raw))
    resolved = os.path.realpath(raw)
    _refuse_overlap(profile, os.path.normcase(resolved))
    return ActorAccount(Path(resolved))


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
        began = time.monotonic()
        with contextlib.suppress(Exception):
            await process.stdin.aclose()
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
    pid: int | None = None
    tool: dict[str, Any] | None = None
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
) -> HostSession:
    """Initialize, call the read tool once, then quit the way a host does."""
    session = HostSession()

    def remember(line: str) -> None:
        session.stderr.append(line)
        on_stderr(line)

    transport = HostQuitTransport(command, env=env, cwd=cwd, on_stderr=remember)
    client = Client(transport, init_timeout=_INIT_SECONDS)
    try:
        async with client:
            result = await client.call_tool_mcp(
                READ_TOOL, READ_TOOL_ARGUMENTS, timeout=_CALL_SECONDS
            )
            session.tool = tool_summary(result)
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
    session.exited_on_quit = transport.exited_on_quit
    session.quit_seconds = transport.quit_seconds
    session.stderr_closed = transport.stderr_closed
    session.killed_by_harness = transport.killed_by_harness
    if transport.process is not None:
        session.exit_code = transport.process.returncode
    return session


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
    """Cleanup only: kill whatever still runs on this row's temporary profile."""
    killed = []
    for process in _profile_processes(account):
        with contextlib.suppress(psutil.Error):
            process.kill()
            killed.append(process.pid)
    return killed


def _is_owner(process: psutil.Process) -> bool:
    with contextlib.suppress(psutil.Error):
        return "linkedin_mcp_server.daemon_owner" in " ".join(process.cmdline())
    return False


def stop_owner(pid: int) -> bool:
    """Kill an owner by its descriptor pid, and only if that pid is still one."""
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return False
    if not _is_owner(process):
        return False
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    else:
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    return True


@dataclass(frozen=True)
class DaemonCleanup:
    directory: str
    existed: bool
    stopped_owner: bool
    cleaned: bool


def retire_daemon_state(account: ActorAccount) -> DaemonCleanup:
    """Remove this row's daemon directory, and nothing else under the state root."""
    directory = daemon_descriptor.daemon_dir(account.auth_root)
    existed = directory.exists()
    stopped = False
    if existed:
        try:
            published = daemon_descriptor.read(account.auth_root)
        except Exception:
            published = None
        if published is not None and published.pid:
            stopped = stop_owner(published.pid)
            if stopped:
                with contextlib.suppress(psutil.Error):
                    psutil.Process(published.pid).wait(timeout=15)
        shutil.rmtree(directory, ignore_errors=True)
    return DaemonCleanup(str(directory), existed, stopped, not directory.exists())


# --- Row H-R1 ----------------------------------------------------------------


@dataclass(frozen=True)
class RowVector:
    """What K0 compares across repeats. O1 and O4 are what K3 compares to K1."""

    #: O1: at no sample more than one browser root on the row's profile.
    o1_single_browser: bool
    #: The watcher's positive control: it saw the browser at all.
    browser_seen: bool
    #: O4: the R17 outcome.
    o4_session: str
    origin_saw_feed: bool
    #: The row's ``/feed/`` requests carried the staged ``li_at``.
    feed_carried_session: bool
    tool_succeeded: bool


def row_expectations(vector: RowVector) -> list[str]:
    """What H-R1 requires of either mode; each unmet one is a failure."""
    failures = []
    if not vector.browser_seen:
        failures.append("the watcher never saw a browser on the row's profile")
    if not vector.o1_single_browser:
        failures.append("O1: a second browser ran on the profile")
    if not vector.origin_saw_feed:
        failures.append("the synthetic origin logged no /feed/ request in the row")
    if not vector.feed_carried_session:
        failures.append("no /feed/ request in the row carried the staged li_at")
    if not vector.tool_succeeded:
        failures.append(f"{READ_TOOL} did not return the synthetic post")
    if vector.o4_session != RETAINED:
        failures.append(f"O4: the session was {vector.o4_session}, not retained")
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
    """K3 against K1: O1 and O4 must be ``=``."""
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
    failures: list[str] = field(default_factory=list)

    def report(self) -> str:
        lines = [f"{self.experiment} ({self.mode}) failures:"]
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
        return "\n".join(lines)


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
    result = RowResult(experiment=experiment, mode="daemon" if daemon else "direct")

    def emit(actor: str, kind: str, **fields: Any) -> None:
        log.emit(experiment=experiment, row=row, actor=actor, kind=kind, **fields)

    await stage_signed_in_session(account.profile)
    # The staging browser has confirmed its close, but a root still on the
    # profile when the watcher takes its baseline would count against O1.
    lingering = await asyncio.to_thread(
        wait_for_no_browser, account, _BROWSER_GONE_SECONDS
    )
    if lingering:
        raise RuntimeError(f"the staging browser is still running: {lingering}")
    result.before = snapshot(account.profile)
    emit(
        "harness", "profile.snapshot", phase="before", **result.before.as_event_fields()
    )

    request_mark, decision_mark = len(origin.requests), len(proxy.decisions)
    env = actor_environment(
        account,
        proxy.url,
        daemon=daemon,
        browsers=Path(
            os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default_browsers_path()
        ),
    )
    work_dir.mkdir(parents=True, exist_ok=True)
    watcher = Watcher(work_dir, log, experiment=experiment, row=row)
    watcher.start()

    owner: dict[str, Any] = {}
    owner_process: psutil.Process | None = None

    async def find_the_owner() -> None:
        nonlocal owner_process
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
        with contextlib.suppress(psutil.Error):
            owner_process = psutil.Process(published.pid)
            owner["start_identity"] = owner_process.create_time()
        emit("harness", "owner.found", **owner)

    after: ProfileSnapshot | None = None
    try:
        host = await run_host_session(
            command or server_command(),
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

        if owner_process is not None:
            began = time.monotonic()
            exit_record: dict[str, Any] = {}
            owner["exit"] = exit_record
            try:
                await asyncio.to_thread(
                    owner_process.wait,
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
        if residual:
            result.failures.append(
                f"a browser on the profile outlived the row by "
                f"{_BROWSER_GONE_SECONDS}s: {residual}"
            )
        after = snapshot(account.profile)
        result.after = after
        emit("harness", "profile.snapshot", phase="after", **after.as_event_fields())
    finally:
        result.cleanup = retire_daemon_state(account)
        swept = sweep_browsers(account)
        result.watcher = watcher.stop()
        for request in origin.requests[request_mark:]:
            emit(
                "origin",
                "browser.request",
                t=request.t or None,
                host=request.host,
                server_name=request.server_name,
                path=request.path,
                cookie_names=list(request.cookie_names),
            )
        for decision in proxy.decisions[decision_mark:]:
            emit(
                "proxy",
                "proxy.decision",
                method=decision.method,
                target=decision.target,
                host=decision.host,
                port=decision.port,
                forwarded=decision.forwarded,
            )
        if swept:
            result.failures.append(f"cleanup had to kill browsers: {swept}")

    result.owner = owner or None
    host = result.host
    assert host is not None

    if host.error is not None:
        result.failures.append(f"the host session failed: {host.error}")
    if not host.exited_on_quit:
        result.failures.append(
            f"the server did not exit within {_HOST_EXIT_SECONDS}s of stdin EOF"
        )
    forwarded = any(_FORWARDING_LINE in line for line in host.stderr)
    if daemon:
        if not owner.get("pid"):
            result.failures.append(
                f"daemon mode published no owner descriptor "
                f"({owner.get('read_error') or 'none on disk'})"
            )
        if not forwarded:
            result.failures.append("the frontend never forwarded to the owner")
        if owner.get("pid") and owner.get("exit", {}).get("how") != "exited":
            result.failures.append(
                f"the owner was still running {_OWNER_EXIT_SLACK_SECONDS}s past "
                f"its {IDLE_TIMEOUT_SECONDS}s idle timeout; cleanup stopped it"
            )
    else:
        if forwarded or owner.get("descriptor_present"):
            result.failures.append("Direct mode reached a shared owner")
    if not result.cleanup.cleaned:
        result.failures.append(
            f"the row's daemon directory survived cleanup: {result.cleanup.directory}"
        )

    watcher_summary = result.watcher or {}
    if not watcher_summary:
        result.failures.append("the watcher wrote no summary")
    most_roots = (watcher_summary.get("max_roots") or {}).get(account.browser_key, 0)
    row_feed = feed_requests(origin.requests[request_mark:])
    user_lines = list(host.stderr)
    if host.tool is not None:
        user_lines += host.tool["text"].splitlines()
    result.vector = RowVector(
        o1_single_browser=bool(watcher_summary) and most_roots <= 1,
        browser_seen=most_roots >= 1,
        o4_session=(
            r17_outcome(result.before, after, user_lines)
            if after is not None
            else "uncertain"
        ),
        origin_saw_feed=bool(row_feed),
        feed_carried_session=any("li_at" in r.cookie_names for r in row_feed),
        tool_succeeded=(
            host.tool is not None
            and not host.tool["is_error"]
            and host.tool["read_the_post"]
        ),
    )
    result.failures += row_expectations(result.vector)
    emit(
        "harness",
        "row.outcome",
        mode=result.mode,
        vector=asdict(result.vector),
        failures=result.failures,
        cleanup=asdict(result.cleanup),
    )
    return result
