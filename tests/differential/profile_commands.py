"""Profile commands beside a live owner (H-R10a, H-R10b) and ``--status``
with one (H-R15).

**The command is the product's own.** Each one is the row's own command line
with ``--logout``, ``--login``, ``--import-from-browser`` or ``--status``,
in the row's own environment, run as a child of the harness
(``TerminalCommand``). The confirmed retirement asks only on an interactive
terminal (``config.is_interactive``: stdin *and* stdout a TTY), so a row
that answers it runs the command on a pseudo-terminal: its stdin, stdout
and stderr are the terminal's slave side, the harness reads the master side
and writes an answer only once the prompt it answers has been read. The
banner the CLI prints only when interactive is what shows the command saw a
terminal. Reads and writes are bounded; the transcript is kept as it came,
each line stamped on the monotonic clock, and nothing in it is a secret: the
commands print paths, versions and their own messages. POSIX only: Windows
has no pseudo-terminal without a new dependency, so there the cells that
need one are counted skips (``NO_TERMINAL_ON_WINDOWS``) and confirmed
retirement stays with the manual protocol's profile-command steps. A
command on pipes (``--status``, and the no-terminal cell) runs everywhere.

**H-R10a, logout beside an idle owner** (``ROW_LOGOUT``). K1 frozen: the
host reads and quits, the Direct server and its browser settle by
themselves (read, never swept), and then ``--logout`` on the terminal,
deletion confirmed: the session is cleared. K3: the host reads through the
owner and quits; the owner stays alive and idle, the row's idle timeout far
from running out; ``--logout`` on the terminal, deletion confirmed, then a
checkpoint showing the owner alive and holding the profile right before the
retirement is confirmed; the owner retires on that request (its exit, and
its own stand-down line, never its idle line), and the logout clears once
the profile is free. Both expect R17 ``cleared-by-user``: the row records
the confirmation (``LOGOUT``) and R17 reads the clear from the artefacts.
Nothing that could sign in again runs after it (``must-remain-cleared``).

Two daemon cells beside it, K1 not applicable (a Direct server records no
shared browser, so nothing asks about retiring one): **retirement
declined** (``ROW_DECLINE``, terminal): exit 0, no retirement request (the
idle owner, which would have retired on one, is still the same lifetime
holding the profile, and its log has no stand-down line), no operation, the
session retained; and **no terminal** (``ROW_NO_TERMINAL``, pipes): the
command says retirement needs a terminal, sends nothing, and the ordinary
profile checks decide. A refusal there is asserted only when checkpoints
around the run show the profile positively held; elsewhere the outcome is
recorded.

**H-R10b, the same commands beside a busy owner** (``ROW_BUSY``, POSIX). The
host's person read is held at the origin, a section at a time, and while
each section is held one command runs with every confirmation: logout,
then login, then import. K3: each is refused on the busy path (409), exit
1, its message names no process, nothing is deleted or rotated, no browser
is launched, the owner is untouched; the import's refusal comes before any
browser profile is discovered or read (no keychain notice, no discovery or
extraction line), from a discovery root that is a disposable synthetic
browser of the row's own (``synthetic_browser``). K1 frozen follows the
frozen Direct's own policy at its lease: logout and import refuse there,
and login may wait for the lease, which the row bounds and ends itself
before the held read is released, so the login can never take the profile.
Each section is released only after its command ended; the held read then
completes normally. Queued-busy and every other branch the native cells do
not reach are mapped to their models (``MODEL_COVERAGE``).

**H-R15, ``--status`` beside a live owner** (``ROW_STATUS``, every
platform, on pipes). The host reads and stays open; a checkpoint, the
status, another checkpoint, and the host reads again. Exit 1 with the
contention category (K3 the product's own two lines, K1 frozen its own old
wording: categories compared, never text), no browser launched, no origin
request inside the status run, the session unchanged. A successful status
while the profile was not shown held around the run is a recorded timing
branch, invalid for the contention claim, not a finding; a successful
status while it was shown held is a finding.

K2 is recorded not applicable for every row here (``K2_NOT_APPLICABLE``).
The scripts run on a ``harness.RowContext`` with its ``commands`` seams;
nothing here reads a process, and the verdicts read the raw record alone,
so each can be replayed from the published packet. Invalid evidence starts
with ``INVALID``, apart from a finding.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from differential import model_coverage
from differential.call_loss import (
    CALIBRATION_IDLE_TIMEOUT_SECONDS,
    ENTRY_SECONDS,
    INVALID,
    LEASE_UNOBSERVED,
    PERSON_TOOL,
    _phase,
    _read_of,
    _settled,
)
from differential.host_comparison import host_problems, same_lifetime
from differential.lease_probe import HELD
from differential.owner_loss import (
    _identified,
    _identity,
    _launches,
    _mapping,
    _ns,
    _owner_kept,
    _sequence,
    _settle_tasks,
)
from differential.retirement_race import (
    WARM_TOOL,
    _calls,
    _page_problems,
    _read_ok,
    _session_problems,
)
from differential.session import CLEARED_BY_USER, LOGOUT, RETAINED
from differential.synthetic_origin import (
    ALLOWED_HOSTS,
    DEADLINE,
    GATE_DEADLINE_SECONDS,
    PEER_GONE,
    RELEASED_BY_ROW,
    SERVED,
    Gate,
    person_path,
)
from linkedin_mcp_server.browser_import.discovery import SUPPORTED_BROWSERS
from linkedin_mcp_server.browser_import.extract import _WINDOWS_EPOCH_OFFSET_SECONDS
from linkedin_mcp_server.cli_main import (
    _RETIRE_BUSY,
    _RETIRE_NEEDS_A_TERMINAL,
    _STATUS_HELD_BY_SHARED_BROWSER,
    _STATUS_PROFILE_HELD,
)

if TYPE_CHECKING:
    from differential.harness import CommandSeams, RowContext

ROW_LOGOUT = "H-R10a-logout"
ROW_DECLINE = "H-R10a-decline"
ROW_NO_TERMINAL = "H-R10a-no-terminal"
ROW_BUSY = "H-R10b"
ROW_STATUS = "H-R15"

#: The calibration's idle timeout, the same in K1, K3 and K0: far above the
#: time from the warm-up read to a command's answer, so the owner cannot
#: idle out first, and the configuration the other call rows measured.
COMMAND_IDLE_TIMEOUT_SECONDS = CALIBRATION_IDLE_TIMEOUT_SECONDS

K2_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "the old refusal is a behaviour difference under the contract's policy "
        "equivalence, not an O1-O4 regression"
    ),
}
#: Why the declined and no-terminal cells have no K1 column.
K1_NOT_APPLICABLE = {
    "status": "not applicable",
    "reason": (
        "a Direct server records no shared browser, so the command asks nothing "
        "about retiring one; the cell is the daemon's own"
    ),
}
NO_TERMINAL_ON_WINDOWS = (
    "Windows has no pseudo-terminal without a new dependency, so confirmed "
    "retirement is not observed natively there; the manual protocol's "
    "profile-command steps (W-protocol) cover it"
)

#: What every command child gets over the row's environment, by name in the
#: record: text in UTF-8 whatever the console's code page, so a Windows
#: status on pipes is measured on its policy and not on its encoding, and
#: output unbuffered, so a line on pipes is stamped when it was written.
COMMAND_ENV = {"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
#: Where a command carries its marker (``TerminalCommand.marker``). Every
#: process it starts inherits it, so the teardown can find them whatever
#: became of their ancestry; only a process that clears its own environment
#: escapes, and no command here starts one.
COMMAND_MARKER_ENV = "LINKEDIN_MCP_DIFFERENTIAL_COMMAND_MARKER"
#: How long the teardown keeps scanning for a member of a command's process
#: group that the last scan missed. One started between a scan's snapshot
#: and its reading is found by the next scan, milliseconds later.
RESCAN_SECONDS = 2.0

LOGOUT_ARGS = ("--logout",)
LOGIN_ARGS = ("--login",)
#: The import's selector: one browser, whose discovery root is the row's
#: disposable synthetic one.
IMPORT_BROWSER = "chrome"
IMPORT_ARGS = ("--import-from-browser", IMPORT_BROWSER)
STATUS_ARGS = ("--status",)

# --- What the commands print -------------------------------------------------------

#: The banner ``main`` prints only when ``config.is_interactive``, in both
#: revisions: the command's own word that it saw a terminal.
BANNER = "🔗 LinkedIn MCP Server v"
#: The deletion prompt, the same in both revisions.
DELETE_PROMPT = "Are you sure you want to clear the profile? (y/N): "
#: The end of ``cli_main._RETIRE_PROMPT``.
RETIRE_PROMPT = "Ask it to retire and continue? (y/N): "
RETIRING_LINE = "The shared browser is retiring; waiting for it to let go."
CLEARED_LINE = "LinkedIn authentication state cleared successfully"
CANCELLED_LINE = "Operation cancelled"
NEEDS_TERMINAL_LINE = _RETIRE_NEEDS_A_TERMINAL
BUSY_LINE = _RETIRE_BUSY
#: ``session_state._exclusive_profile`` refusing a held profile, in both.
LEASE_REFUSAL = "The browser profile is in use by another process"
#: The frozen import and login refusing at their lease (``BrowserBusyError``).
IMPORT_LEASE_REFUSAL = "so a session cannot be imported"
LOGIN_LEASE_REFUSAL = "so a login cannot start"
LOGIN_BANNER = "LinkedIn MCP Server - Profile Creation"
#: What a login that took the profile prints next, and an import that read a
#: browser's cookies: none of these may follow a refusal.
LOGIN_OPENED = "Opening browser for LinkedIn login"
PROFILE_SAVED = "Profile saved to"
KEYCHAIN_NOTICE = "may prompt to allow keychain access"
IMPORT_FOUND = "browser profile(s) with a live LinkedIn session"
IMPORT_VALIDATING = "LinkedIn cookies from"
IMPORTED = "Imported and validated LinkedIn session"
#: A command that had to install its browser first: not what is judged here.
INSTALLING = "Installing Patchright Chromium browser"

#: The owner's own lines (``daemon_owner``), in its log.
STANDING_DOWN_LINE = "A profile command asked for the browser; standing down"
IDLE_EXIT_LINE = "Nothing has needed the browser in"

#: ``--status``: the candidate's two lines, and every other ending by the
#: words each revision prints.
STATUS_HELD = _STATUS_PROFILE_HELD
STATUS_SHARED = _STATUS_HELD_BY_SHARED_BROWSER
#: The frozen baseline's ``BrowserBusyError`` text, under its generic
#: "Could not validate session" (``0253421`` ``exceptions.py``).
FROZEN_CONTENTION = "is currently using the browser"
STATUS_VALID = "Session is valid"
STATUS_EXPIRED = "Session expired or invalid"
STATUS_NO_SESSION = "No valid source session found"
STATUS_BRIDGE = "Source cookie validity is not verified"

CONTENTION = "contention"
VALID = "valid"
EXPIRED = "expired"
NO_SESSION = "no-session"
BRIDGE = "bridge"
OTHER = "other"

# --- Bounds ----------------------------------------------------------------------

#: From a command's start to its first prompt: an interpreter and the
#: product's imports on the slowest runner.
PROMPT_SECONDS = 60.0
#: From the last answer to a refusal's exit.
REFUSAL_SECONDS = 15.0
#: From the confirmed retirement to the logout's exit: the product's own
#: wait for the profile (``PROFILE_HANDOVER_WAIT_SECONDS``, 60s) and the
#: clear.
CONFIRMED_SECONDS = 90.0
#: How long the end of a command's output may lag its exit.
OUTPUT_END_SECONDS = 10.0
#: How long before the retirement answer the checkpoint that clears it
#: may have ended.
FRESH_SECONDS = 10.0
#: How long the declined and no-terminal cells watch the owner after the
#: command, for a retirement nobody should have asked for.
WATCH_SECONDS = 5.0
#: K1 frozen ``--login`` waits for the lease (60s); the row watches it wait
#: this long and then ends it, the read still held.
LOGIN_WAIT_SECONDS = 4.0
#: The latest a command may still run in a held section, from the hold's
#: entry: two seconds inside the gate's deadline.
HOLD_CAP_SECONDS = GATE_DEADLINE_SECONDS - 2.0
#: ``--status`` from its start to its exit.
STATUS_SECONDS = 90.0
#: The held read's end after the last release: a section and its delay.
READ_END_SECONDS = 120.0

#: H-R10b's held read, its own username.
BUSY_USERNAME = "synthetic-busy"
#: Each command and the section of the read held while it runs.
BUSY_COMMANDS = (
    ("logout", LOGOUT_ARGS, "main_profile"),
    ("login", LOGIN_ARGS, "experience"),
    ("import", IMPORT_ARGS, "education"),
)


@dataclass(frozen=True)
class CommandCase:
    """One row here: its script, the R17 outcome it expects, and whether
    every cell of it needs a terminal and whether it has a K1 column."""

    script: Callable[[RowContext], Awaitable[None]]
    expect_session: str
    terminal: bool
    direct: bool


# --- The driver ------------------------------------------------------------------


#: The terminal's size: wide enough that no prompt or message is wrapped.
TERMINAL_ROWS, TERMINAL_COLUMNS = 50, 400


def _wide(fd: int) -> None:
    import fcntl
    import struct
    import termios

    size = struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, size)


class TerminalCommand:
    """One profile command as a child of the harness, on a pseudo-terminal
    or on pipes, its output kept as it came.

    On a terminal the child's stdin, stdout and stderr are the slave side of
    a fresh pseudo-terminal, which the parent closes once the child holds
    it; the parent reads the master side. On pipes, stdin is a pipe and
    stdout and stderr one more. A thread of its own reads until the output
    ends; nothing is ever written but an answer the row gives. On POSIX the
    command leads a process group of its own, as a shell's job does, which
    names what it starts apart from its environment (``_marked``). It is
    signalled only by ``interrupt`` (the row's own Ctrl-C) and ``end`` (the
    teardown's, after which nothing about it counts as settled by itself).
    """

    def __init__(
        self,
        argv: Sequence[str],
        *,
        args: Sequence[str],
        env: Mapping[str, str],
        cwd: Path,
        terminal: bool,
        label: str,
        overridden: Sequence[str] = (),
    ) -> None:
        if terminal and os.name == "nt":
            raise ValueError(NO_TERMINAL_ON_WINDOWS)
        self.argv = list(argv)
        self.args = list(args)
        #: A value only this command's process tree carries, inherited by
        #: every process it starts, however it detaches: the authoritative
        #: boundary of what it started (``_marked``). Fresh per command.
        self.marker = secrets.token_hex(16)
        self.env = {**env, COMMAND_MARKER_ENV: self.marker}
        self.cwd = Path(cwd)
        self.terminal = terminal
        self.label = label
        self.overridden = list(overridden)
        self.process: subprocess.Popen[bytes] | None = None
        self._fd: int | None = None
        self._chunks: list[tuple[int, str]] = []
        self._lock = threading.Lock()
        self._reader: threading.Thread | None = None
        #: Guards the terminal's descriptor alone, apart from the output.
        self._fd_lock = threading.Lock()
        self._closed = False
        self.started_ns: int | None = None
        self.exited_ns: int | None = None
        self.returncode: int | None = None
        self.interrupted_ns: int | None = None
        self.ended_by_harness = False
        self.error: str | None = None
        self.answers: list[dict[str, Any]] = []
        self.expected: list[dict[str, Any]] = []
        #: Every descendant seen while the command ran, by its lifetime
        #: ``(pid, create time)``: what the command started, which outlives
        #: its exit and its output (a browser it launched, a helper with its
        #: own output) and must be settled too.
        self.descendants: dict[tuple[int, float], Any] = {}
        #: The command's process group on POSIX, its own pid; None on Windows,
        #: and None once the kernel said the group is empty (``_group_empty``):
        #: an empty group frees its number for reuse, so it is never read again.
        self.group: int | None = None

    def _collect(self) -> None:
        """Record the command's descendants now. Only while it is alive: once
        it exits they are reparented, and nothing names them as its any more,
        so every poll before the exit collects."""
        import psutil

        if self.process is None or self.process.poll() is not None:
            return
        try:
            children = psutil.Process(self.process.pid).children(recursive=True)
        except psutil.Error:
            return
        for child in children:
            try:
                self.descendants.setdefault((child.pid, child.create_time()), child)
            except psutil.Error:
                continue

    def _marked(self) -> None:
        """Record every process now in the command's process group or
        carrying its marker: what ancestry cannot find, a helper started just
        before the command exits or one that left the tree.

        Each covers what the other cannot read. A process inherits the
        marker however it detaches, double fork and new session included,
        but an environment is not always readable from outside: macOS hands
        back an empty one for its own restricted binaries (``/bin/sleep``,
        ``/usr/bin/security``), and Linux refuses it for a non-dumpable
        process. The group is read from the kernel for any process, and is
        left only by one that starts a group or session of its own. A helper
        that does both, an unreadable binary that leaves the group, escapes;
        no command here starts one."""
        import psutil

        own = os.getpid()
        for process in psutil.process_iter():
            if process.pid == own:
                continue
            try:
                grouped = (
                    self.group is not None and os.getpgid(process.pid) == self.group
                )
            except OSError:
                grouped = False
            try:
                if (
                    not grouped
                    and process.environ().get(COMMAND_MARKER_ENV) != self.marker
                ):
                    continue
                key = (process.pid, process.create_time())
            except (psutil.Error, OSError):
                continue
            self.descendants.setdefault(key, process)

    def _group_empty(self) -> bool:
        """Whether the command's process group has no member left, asked of
        the kernel in one step. A scan of the process table cannot answer
        this: a member may start another and exit between the scan's
        snapshot and its reading of each process, so neither is seen while
        the group is still occupied. Only an empty group retires it, and a
        group with a member no scan found is not settled."""
        if self.group is None:
            return True
        try:
            os.killpg(self.group, 0)
        except ProcessLookupError:
            self.group = None
            return True
        except PermissionError:
            # A member exists that this user may not signal.
            return False
        return False

    def _alive_descendants(self) -> list[tuple[int, float]]:
        """The descendants recorded or now marked, still running as the same
        lifetime: the command itself excluded."""
        import psutil

        self._marked()
        alive = []
        own = self.process.pid if self.process is not None else None
        for key, process in self.descendants.items():
            if key[0] == own and self.returncode is None:
                continue
            try:
                if process.is_running() and process.create_time() == key[1]:
                    if process.status() != psutil.STATUS_ZOMBIE:
                        alive.append(key)
            except psutil.Error:
                continue
        return alive

    def start(self) -> None:
        # Read before the child exists, so nothing it prints can be stamped
        # earlier than its start.
        self.started_ns = time.monotonic_ns()
        if self.terminal:
            import pty

            master, slave = pty.openpty()
            _wide(slave)
            try:
                self.process = subprocess.Popen(
                    self.argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=self.env,
                    cwd=self.cwd,
                    close_fds=True,
                    process_group=0,
                )
            except BaseException:
                os.close(master)
                self.started_ns = None
                raise
            finally:
                os.close(slave)
            self._fd = master
        else:
            try:
                self.process = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=self.env,
                    cwd=self.cwd,
                    process_group=None if os.name == "nt" else 0,
                )
            except BaseException:
                self.started_ns = None
                raise
            assert self.process.stdout is not None
            self._fd = self.process.stdout.fileno()
        if os.name != "nt" and self.process is not None:
            self.group = self.process.pid
        self._reader = threading.Thread(
            target=self._read, name=f"profile command {self.label}", daemon=True
        )
        self._reader.start()

    def _read(self) -> None:
        """Every byte until the output ends: EOF, or on Linux ``EIO`` once the
        terminal's last slave descriptor is closed."""
        assert self._fd is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                try:
                    data = os.read(self._fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                self._note(decoder.decode(data))
        finally:
            self._note(decoder.decode(b"", final=True))
            if self.terminal:
                # Closed under the lock an answer writes under, so no answer
                # ever reaches a descriptor number reused by then.
                with self._fd_lock:
                    os.close(self._fd)
                    self._closed = True
            elif self.process is not None and self.process.stdout is not None:
                self.process.stdout.close()

    def _note(self, text: str) -> None:
        # A terminal ends lines with CR LF and echoes the answers back; the
        # carriage returns carry nothing a verdict reads.
        text = text.replace("\r", "")
        if text:
            with self._lock:
                self._chunks.append((time.monotonic_ns(), text))

    def transcript(self) -> str:
        with self._lock:
            return "".join(text for _, text in self._chunks)

    def mark(self) -> int:
        """Where the transcript ends now, for an ``expect`` of what follows."""
        return len(self.transcript())

    def arrival(self, text: str, start: int = 0) -> int | None:
        """When *text* first stood whole in the transcript past *start*: the
        monotonic time of the chunk that completed it, or None."""
        with self._lock:
            chunks = list(self._chunks)
        seen = ""
        for at, chunk in chunks:
            seen += chunk
            if text in seen[start:]:
                return at
        return None

    async def expect(self, text: str, seconds: float, *, start: int = 0) -> int | None:
        """Wait up to *seconds* for *text* past *start*; when it arrived."""
        deadline = time.monotonic() + seconds
        while True:
            at = self.arrival(text, start)
            if at is not None or time.monotonic() >= deadline:
                self.expected.append({"text": text, "seen_ns": at})
                return at
            if self.returncode is None and self.process is not None:
                self._poll()
            if self.returncode is not None and not self._reading():
                at = self.arrival(text, start)
                self.expected.append({"text": text, "seen_ns": at})
                return at
            await asyncio.sleep(0.02)

    def answer(self, text: str) -> dict[str, Any]:
        """Type *text* and Enter, once; its record. Its time is read before
        the write, so nothing the answer causes can be stamped earlier."""
        found: dict[str, Any] = {"text": text, "answered_ns": None, "error": None}
        data = f"{text}\n".encode()
        at = time.monotonic_ns()
        try:
            if self.terminal:
                with self._fd_lock:
                    if self._closed or self._fd is None:
                        raise OSError("the terminal is closed")
                    os.write(self._fd, data)
            else:
                assert self.process is not None and self.process.stdin is not None
                self.process.stdin.write(data)
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            found["error"] = f"{type(exc).__name__}: {exc}"
        else:
            found["answered_ns"] = at
        self.answers.append(found)
        return found

    def interrupt(self) -> dict[str, Any]:
        """The row's own Ctrl-C to the command, as a terminal would send it.

        Sent to the command's own process. A SIGINT the harness was started
        with ignored (a shell's background job) stays ignored in the command,
        which then does not end; the teardown's end makes that cell invalid.
        """
        found: dict[str, Any] = {"interrupted_ns": None, "error": None}
        try:
            assert self.process is not None
            self.process.send_signal(signal.SIGINT)
        except (OSError, ValueError, AssertionError) as exc:
            found["error"] = f"{type(exc).__name__}: {exc}"
        else:
            self.interrupted_ns = found["interrupted_ns"] = time.monotonic_ns()
        return found

    def _poll(self) -> None:
        assert self.process is not None
        self._collect()
        code = self.process.poll()
        if code is not None and self.returncode is None:
            self.returncode = code
            self.exited_ns = time.monotonic_ns()

    def _reading(self) -> bool:
        return self._reader is not None and self._reader.is_alive()

    async def wait(self, seconds: float) -> int | None:
        """Wait up to *seconds* for the command's exit; when it was seen."""
        if self.process is None:
            return None
        deadline = time.monotonic() + seconds
        while self.returncode is None:
            self._poll()
            if self.returncode is not None or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.02)
        return self.exited_ns

    def settled(self, grace: float) -> bool:
        """Whether the command has exited, its output ended and every
        descendant it was seen to start is gone, waiting up to *grace* for
        the output and the descendants. Never signals."""
        if self.process is None:
            return True
        if self.returncode is None:
            self._poll()
        if self.returncode is None:
            return False
        # One bound for both waits, so a caller's own bound holds.
        deadline = time.monotonic() + grace
        if self._reader is not None:
            self._reader.join(grace)
        while self._occupied() and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self._reading() and not self._occupied()

    def _occupied(self) -> bool:
        """Whether any descendant still runs, found or only known to be in
        the group."""
        return bool(self._alive_descendants()) or not self._group_empty()

    def end(self) -> None:
        """The teardown's: kill a command still running, and every descendant
        it was seen to start that still runs as the same lifetime, and wait
        for them. Only processes it recorded, each checked against its
        creation time first; never a process group."""
        if self.process is not None and self.process.poll() is None:
            self.ended_by_harness = True
            self._collect()
            try:
                self.process.kill()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self.error = f"{type(exc).__name__}: {exc}"
            self._poll()
        # Until the group is empty too: a member one scan missed is found by
        # the next, and only a found one is ever signalled. Bounded, since a
        # member no scan can see is never found; the command then stays
        # unsettled and says so.
        deadline = time.monotonic() + RESCAN_SECONDS
        while True:
            left = self._alive_descendants()
            if not left and self._group_empty():
                return
            if time.monotonic() >= deadline:
                self.error = (
                    f"descendants left after the teardown: {left}; process group "
                    f"{self.group} {'empty' if self._group_empty() else 'occupied'}"
                )
                return
            self.ended_by_harness = self.ended_by_harness or bool(left)
            for key in left:
                process = self.descendants[key]
                try:
                    if process.create_time() == key[1]:
                        process.kill()
                        process.wait(timeout=10)
                except Exception as exc:  # noqa: BLE001 - recorded; the next goes on
                    self.error = f"{type(exc).__name__}: {exc}"
            if not left:
                time.sleep(0.05)

    def lines(self) -> list[tuple[int, str]]:
        """Each line of the transcript with when its end arrived; an
        unfinished last line, such as a prompt, counts as one."""
        with self._lock:
            chunks = list(self._chunks)
        found: list[tuple[int, str]] = []
        current = ""
        for at, chunk in chunks:
            parts = chunk.split("\n")
            for part in parts[:-1]:
                found.append((at, current + part))
                current = ""
            current += parts[-1]
        if current:
            found.append((chunks[-1][0], current))
        return found

    def record(self) -> dict[str, Any]:
        if self.process is not None and self.returncode is None:
            self._poll()
        return {
            "label": self.label,
            "args": list(self.args),
            "terminal": self.terminal,
            "overridden": sorted({*COMMAND_ENV, *self.overridden}),
            "started_ns": self.started_ns,
            "exited_ns": self.exited_ns,
            "returncode": self.returncode,
            "interrupted_ns": self.interrupted_ns,
            "ended_by_harness": self.ended_by_harness,
            "output_ended": self.process is not None and not self._reading(),
            "descendants": [list(key) for key in self.descendants],
            "descendants_alive": [list(key) for key in self._alive_descendants()],
            "group_occupied": not self._group_empty(),
            "error": self.error,
            "expected": [dict(item) for item in self.expected],
            "answers": [dict(item) for item in self.answers],
            "lines": [[at, line] for at, line in self.lines()],
        }


# --- The import's synthetic browser ----------------------------------------------


def _discovery_base(scratch: Path) -> tuple[Path, dict[str, str]]:
    """Where the import looks for browsers under *scratch*, and the
    environment that sends it there (``discovery._os_base_dirs``)."""
    if sys.platform == "darwin":
        home = scratch / "home"
        return home / "Library" / "Application Support", {"HOME": str(home)}
    if os.name == "nt":
        local = scratch / "localappdata"
        return local, {"LOCALAPPDATA": str(local), "APPDATA": str(scratch / "appdata")}
    config = scratch / "config"
    return config, {"XDG_CONFIG_HOME": str(config)}


def synthetic_browser(scratch: Path) -> dict[str, str]:
    """A disposable Chrome profile the import's discovery finds and ranks
    live, and the environment that points discovery at it, never at a real
    browser's profile.

    ``Local State`` names one profile, ``Default``, whose ``Cookies``
    database holds one ``li_at`` on ``.linkedin.com`` a year from expiry,
    in plaintext, with a random value that means nothing: ranking reads
    only its expiry. Nothing reads further unless the import took the
    profile.
    """
    base, environment = _discovery_base(scratch)
    spec = SUPPORTED_BROWSERS[IMPORT_BROWSER]
    if sys.platform == "darwin":
        subpath = str(spec["mac_subpath"])
    elif os.name == "nt":
        subpath = str(spec["win_subpath"])
    else:
        subpath = cast("tuple[str, ...]", spec["linux_subpaths"])[0]
    root = base / subpath
    profile = root / "Default"
    profile.mkdir(parents=True, exist_ok=True)
    (root / "Local State").write_text(
        '{"profile": {"info_cache": {"Default": {"name": "Synthetic"}}}}'
    )
    (profile / "Preferences").write_text("{}")
    expires = int((time.time() + 365 * 24 * 3600 + _WINDOWS_EPOCH_OFFSET_SECONDS) * 1e6)
    accessed = int((time.time() + _WINDOWS_EPOCH_OFFSET_SECONDS) * 1e6)
    connection = sqlite3.connect(profile / "Cookies")
    try:
        connection.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
            "encrypted_value BLOB, path TEXT, expires_utc INTEGER, "
            "last_access_utc INTEGER, is_secure INTEGER, is_httponly INTEGER, "
            "samesite INTEGER)"
        )
        connection.execute(
            "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ".linkedin.com",
                "li_at",
                f"synthetic-import-{secrets.token_urlsafe(16)}",
                b"",
                "/",
                expires,
                accessed,
                1,
                1,
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return environment


# --- The scripts -----------------------------------------------------------------


def _owner_actor(ctx: RowContext) -> tuple[Any, int, float] | None:
    owner = ctx.owner()
    return (owner.process, owner.pid, owner.create_time) if owner else None


def _owner_lines(seams: CommandSeams) -> dict[str, int]:
    """How often the owner's log says it stood down for a profile command,
    and that it idled out."""
    lines = seams.owner_log()
    return {
        "standing_down": sum(1 for line in lines if STANDING_DOWN_LINE in line),
        "idle_exit": sum(1 for line in lines if IDLE_EXIT_LINE in line),
    }


def _seams(ctx: RowContext) -> CommandSeams | None:
    if ctx.commands is None:
        ctx.record["observation_problems"].append(
            f"{INVALID}the row was given no way to run a command"
        )
    return ctx.commands


async def logout_script(ctx: RowContext) -> None:
    """H-R10a's three cells, after the warm-up read: the host quits, then
    ``--logout`` runs beside whatever was left."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    case = CASES[ctx.row]
    record.update(command=list(LOGOUT_ARGS), terminal=case.terminal)
    seams = _seams(ctx)
    if seams is None:
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    # The in-process host stub's own quit: stdin EOF and a wait. A row here
    # never runs its host as a process of its own.
    host_quit = getattr(ctx.transport, "host_quit", None)
    if host_quit is None:
        problems.append(f"{INVALID}the row's host cannot be quit from its script")
        return
    await host_quit()
    record["host_quit_ns"] = time.monotonic_ns()
    _phase(ctx, "host quit", record["host_quit_ns"])
    if not ctx.daemon:
        # Read, never swept: a logout on a profile not shown free would race
        # whatever is still on it.
        record["settlement"] = await seams.settlement()
        if not _settled(record["settlement"]):
            problems.append(
                f"{INVALID}the Direct server's profile was not shown settled "
                f"after the host quit; nothing was run on it"
            )
            return
    else:
        record["owner_after_quit"] = await seams.owner_reading("after the host quit")
        if ctx.row == ROW_NO_TERMINAL:
            record["before_command"] = await ctx.checkpoint(
                "before the command", actor=_owner_actor(ctx)
            )
    command = await seams.start(LOGOUT_ARGS, terminal=case.terminal, label="logout")
    try:
        await _answer_logout(ctx, seams, command)
    finally:
        if command.returncode is None:
            await command.wait(REFUSAL_SECONDS)
        record["logout"] = await seams.finish(command, OUTPUT_END_SECONDS)
    _phase(ctx, "command ended", command.exited_ns)
    if ctx.daemon:
        if ctx.row != ROW_LOGOUT:
            await asyncio.sleep(WATCH_SECONDS)
            record["after_command"] = await ctx.checkpoint(
                "after the command", actor=_owner_actor(ctx)
            )
        record["owner_after_command"] = await seams.owner_reading("after the command")
        record["owner_lines"] = _owner_lines(seams)


async def _answer_logout(
    ctx: RowContext, seams: CommandSeams, command: TerminalCommand
) -> None:
    """Confirm the deletion; then, beside an owner on a terminal, check the
    owner and confirm (or decline) its retirement; and wait for the end."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    if await command.expect(DELETE_PROMPT, PROMPT_SECONDS) is None:
        problems.append(
            f"{INVALID}the logout never asked to delete within {PROMPT_SECONDS}s"
        )
        return
    mark = command.mark()
    command.answer("y")
    # The user's confirmation, recorded as such: what R17 needs beside the
    # clear it reads from the artefacts.
    record["authorized"] = LOGOUT
    if not (ctx.daemon and command.terminal):
        await command.wait(REFUSAL_SECONDS if ctx.daemon else CONFIRMED_SECONDS)
        return
    if await command.expect(RETIRE_PROMPT, PROMPT_SECONDS, start=mark) is None:
        # The owner was recorded and the terminal interactive: a command that
        # never asks is the record's finding, not missing evidence.
        await command.wait(REFUSAL_SECONDS)
        return
    record["before_answer"] = await ctx.checkpoint(
        "before the retirement answer", actor=_owner_actor(ctx)
    )
    if ctx.row == ROW_DECLINE:
        command.answer("n")
        await command.wait(REFUSAL_SECONDS)
        return
    command.answer("y")
    _, record["owner_exit"] = await asyncio.gather(
        command.wait(CONFIRMED_SECONDS), seams.owner_exit(CONFIRMED_SECONDS)
    )


async def busy_script(ctx: RowContext) -> None:
    """H-R10b, after the warm-up read: a person read held a section at a
    time, and in each section one command with every confirmation."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    record.update(username=BUSY_USERNAME, terminal=True)
    seams = _seams(ctx)
    if seams is None:
        return
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
    import_env = synthetic_browser(seams.scratch)
    record["import_discovery"] = sorted(import_env)
    gates = {
        section: ctx.hold(person_path(BUSY_USERNAME, section), ordinal=1)
        for _, _, section in BUSY_COMMANDS
    }
    record["held"] = {
        name: {"path": person_path(BUSY_USERNAME, section), "ordinal": 1}
        for name, _, section in BUSY_COMMANDS
    }
    _phase(ctx, "armed")
    read: asyncio.Future[Any] | None = None
    waiting: TerminalCommand | None = None
    try:
        for index, (name, args, section) in enumerate(BUSY_COMMANDS):
            gate = gates[section]
            prompt = _first_prompt(name, daemon=ctx.daemon)
            command = waiting
            if command is None and prompt is not None:
                command = await _ready(ctx, seams, name, args, prompt, import_env)
                if command is None:
                    return
            if read is None:
                _phase(ctx, "read sent")
                read = asyncio.ensure_future(
                    ctx.call(PERSON_TOOL, _read_of(BUSY_USERNAME))
                )
            if not await _entered(gate, read):
                problems.append(
                    f"{INVALID}the {section} page was not held within "
                    f"{ENTRY_SECONDS}s, or the read ended first: no command ran "
                    f"beside a busy owner"
                )
                if command is not None:
                    await seams.finish(command, 0.0)
                return
            entered = gate.entered_monotonic_ns
            assert entered is not None
            _phase(ctx, f"{name} held", entered)
            if command is None:
                command = await seams.start(
                    args,
                    terminal=True,
                    label=name,
                    overrides=import_env if name == "import" else None,
                )
            await _run_busy(ctx, command, name, entered)
            record[name] = await seams.finish(command, OUTPUT_END_SECONDS)
            record[name]["read_open"] = not read.done()
            # The next command waits at its prompt while this section is
            # still held, so its interpreter's start is not the next hold's.
            waiting = None
            following = (
                BUSY_COMMANDS[index + 1] if index + 1 < len(BUSY_COMMANDS) else None
            )
            if following is not None:
                after = _first_prompt(following[0], daemon=ctx.daemon)
                if after is not None:
                    waiting = await _ready(
                        ctx, seams, following[0], following[1], after, import_env
                    )
                    if waiting is None:
                        return
            gate.release(by=RELEASED_BY_ROW)
            record[name]["release_requested_ns"] = gate.release_requested_monotonic_ns
            _phase(ctx, f"{name} released", gate.release_requested_monotonic_ns)
        assert read is not None
        await asyncio.wait({read}, timeout=READ_END_SECONDS)
        record["read_open"] = not read.done()
        _phase(ctx, "read returned")
        if ctx.daemon:
            record["owner_after_read"] = await seams.owner_reading("after the read")
            record["owner_lines"] = _owner_lines(seams)
    finally:
        await _settle_tasks([read])


def _first_prompt(name: str, *, daemon: bool) -> str | None:
    """What a command asks first, which the row waits for before its hold;
    None for one that acts as soon as it starts (K1's login and import)."""
    if name == "logout":
        return DELETE_PROMPT
    return RETIRE_PROMPT if daemon else None


async def _ready(
    ctx: RowContext,
    seams: CommandSeams,
    name: str,
    args: Sequence[str],
    prompt: str,
    import_env: Mapping[str, str],
) -> TerminalCommand | None:
    """Start *name* and wait for its first prompt, unanswered."""
    command = await seams.start(
        args,
        terminal=True,
        label=name,
        overrides=import_env if name == "import" else None,
    )
    if await command.expect(prompt, PROMPT_SECONDS) is None:
        ctx.record["observation_problems"].append(
            f"{INVALID}the {name} command did not ask {prompt!r} within "
            f"{PROMPT_SECONDS}s"
        )
        ctx.record[name] = await seams.finish(command, REFUSAL_SECONDS)
        return None
    return command


async def _entered(gate: Gate, read: asyncio.Future[Any]) -> bool:
    deadline = time.monotonic() + ENTRY_SECONDS
    while not gate.entered.is_set():
        if read.done() or time.monotonic() >= deadline:
            return gate.entered.is_set()
        await asyncio.sleep(0.01)
    return True


def _left(entered: int) -> float:
    """Seconds left before the hold's cap, from its entry."""
    return max(0.0, HOLD_CAP_SECONDS - (time.monotonic_ns() - entered) / 1e9)


async def _run_busy(
    ctx: RowContext, command: TerminalCommand, name: str, entered: int
) -> None:
    """One command inside its held section, every confirmation given."""
    if name == "logout":
        # The deletion is confirmed here as in H-R10a, and recorded so.
        ctx.record["authorized"] = LOGOUT
    if ctx.daemon:
        mark = command.mark()
        command.answer("y")
        if name == "logout":
            # The deletion first; then the retirement it asks about.
            if (
                await command.expect(RETIRE_PROMPT, _left(entered), start=mark)
                is not None
            ):
                command.answer("y")
        await command.wait(min(REFUSAL_SECONDS, _left(entered)))
        return
    if name == "logout":
        command.answer("y")
        await command.wait(min(REFUSAL_SECONDS, _left(entered)))
        return
    if name == "login":
        # The frozen login waits for the lease. Watched waiting, then ended
        # by the row while the read still holds the profile.
        if await command.expect(LOGIN_BANNER, _left(entered)) is not None:
            await command.wait(min(LOGIN_WAIT_SECONDS, _left(entered)))
        if command.returncode is None:
            command.interrupt()
            await command.wait(min(REFUSAL_SECONDS, _left(entered) + 2.0))
        return
    await command.wait(_left(entered))


async def status_script(ctx: RowContext) -> None:
    """H-R15, after the warm-up read, the host open: checkpoint, status,
    checkpoint, and the host reads again."""
    record = ctx.record
    problems: list[str] = record["observation_problems"]
    record.update(command=list(STATUS_ARGS), terminal=False)
    seams = _seams(ctx)
    if seams is None:
        return
    actor = None
    if ctx.daemon:
        record["owner_identified"] = _identity(ctx)
        if record["owner_identified"] is None:
            problems.append(f"{INVALID}the owner was never identified")
            return
        actor = _owner_actor(ctx)
    record["before_status"] = await ctx.checkpoint("before the status", actor=actor)
    record["roots_before"] = await seams.roots("before the status")
    command = await seams.start(STATUS_ARGS, terminal=False, label="status")
    try:
        await command.wait(STATUS_SECONDS)
    finally:
        record["status"] = await seams.finish(command, OUTPUT_END_SECONDS)
    _phase(ctx, "status ended", command.exited_ns)
    record["after_status"] = await ctx.checkpoint("after the status", actor=actor)
    record["roots_after"] = await seams.roots("after the status")
    _phase(ctx, "read again")
    await ctx.call(WARM_TOOL, {"num_posts": 1})
    if ctx.daemon:
        record["owner_after_read"] = await seams.owner_reading("after the second read")


CASES: dict[str, CommandCase] = {
    ROW_LOGOUT: CommandCase(logout_script, CLEARED_BY_USER, terminal=True, direct=True),
    ROW_DECLINE: CommandCase(logout_script, RETAINED, terminal=True, direct=False),
    ROW_NO_TERMINAL: CommandCase(logout_script, RETAINED, terminal=False, direct=False),
    ROW_BUSY: CommandCase(busy_script, RETAINED, terminal=True, direct=True),
    ROW_STATUS: CommandCase(status_script, RETAINED, terminal=False, direct=True),
}
ROWS = tuple(CASES)

# --- Reading a record ------------------------------------------------------------


def _command(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    return _mapping(record.get(name))


def _lines(command: Mapping[str, Any]) -> list[tuple[int | None, str]]:
    found = []
    for entry in _sequence(command.get("lines")):
        entry = _sequence(entry)
        if len(entry) == 2 and isinstance(entry[1], str):
            found.append((_ns(entry[0]), entry[1]))
    return found


def _text(command: Mapping[str, Any]) -> str:
    return "\n".join(line for _, line in _lines(command))


def _first(command: Mapping[str, Any], text: str) -> int | None:
    """When a line holding *text* first arrived (0 where its time is not
    readable), or None when no line holds it."""
    for at, line in _lines(command):
        if text in line:
            return at or 0
    return None


def _answers(command: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [_mapping(answer) for answer in _sequence(command.get("answers"))]


def _seen(command: Mapping[str, Any], text: str) -> int | None:
    for item in _sequence(command.get("expected")):
        item = _mapping(item)
        if item.get("text") == text and _ns(item.get("seen_ns")) is not None:
            return _ns(item.get("seen_ns"))
    return None


def _lock(point: Any) -> str:
    """The lease as a checkpoint's contender answered it; ``unobserved``
    where the platform has none."""
    answer = _mapping(_mapping(_mapping(point).get("lock")).get("answer"))
    state = answer.get("state")
    return state if isinstance(state, str) else LEASE_UNOBSERVED


def _actor_alive(point: Any) -> bool:
    return list(_sequence(_mapping(point).get("actor_alive"))) == [True, True]


def _windows(record: Mapping[str, Any]) -> bool:
    return str(record.get("platform", "")).startswith("win")


def _roots(reading: Any) -> list[list[Any]] | None:
    roots = _mapping(reading).get("roots")
    return (
        [list(_sequence(root)) for root in roots] if isinstance(roots, list) else None
    )


def held_around(record: Mapping[str, Any], before: str, after: str) -> bool | None:
    """Whether the profile was shown held at both checkpoints: True or False
    where the contender answered, None where it could not (Windows)."""
    states = [_lock(record.get(before)), _lock(record.get(after))]
    if LEASE_UNOBSERVED in states:
        return None
    return states == [HELD, HELD]


def status_category(command: Mapping[str, Any]) -> str:
    """How a ``--status`` ended, by the words either revision prints."""
    text = _text(command)
    if STATUS_HELD in text or FROZEN_CONTENTION in text:
        return CONTENTION
    if STATUS_VALID in text:
        return VALID
    if STATUS_EXPIRED in text:
        return EXPIRED
    if STATUS_NO_SESSION in text:
        return NO_SESSION
    if STATUS_BRIDGE in text:
        return BRIDGE
    return OTHER


def logout_outcome(command: Mapping[str, Any]) -> str:
    """What a logout did, by its own words and exit."""
    text = _text(command)
    code = command.get("returncode")
    if CLEARED_LINE in text and code == 0:
        return "cleared"
    if BUSY_LINE in text:
        return "busy-refused"
    if LEASE_REFUSAL in text:
        return "lease-refused"
    if CANCELLED_LINE in text and code == 0:
        return "declined"
    return OTHER


def busy_outcome(name: str, command: Mapping[str, Any]) -> str:
    """How a command beside a busy owner or a held profile ended."""
    text = _text(command)
    if any(
        line in text for line in (CLEARED_LINE, LOGIN_OPENED, PROFILE_SAVED, IMPORTED)
    ):
        return "proceeded"
    if BUSY_LINE in text:
        return "busy-refused"
    if (
        LEASE_REFUSAL in text
        or IMPORT_LEASE_REFUSAL in text
        or LOGIN_LEASE_REFUSAL in text
    ):
        return "lease-refused"
    if name == "login" and _ns(command.get("interrupted_ns")) is not None:
        return "waited"
    return OTHER


def _common(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    problems: list[str] = []
    mode = "daemon" if daemon else "direct"
    if record.get("mode") != mode:
        problems.append(f"the record is for mode {record.get('mode')!r}, not {mode}")
    if record.get("script_error"):
        problems.append(f"the row's script failed: {record['script_error']}")
    problems += [str(p) for p in _sequence(record.get("observation_problems"))]
    if record.get("idle_timeout_seconds") != COMMAND_IDLE_TIMEOUT_SECONDS:
        problems.append(
            f"the row ran with an idle timeout of "
            f"{record.get('idle_timeout_seconds')!r}, not the declared "
            f"{COMMAND_IDLE_TIMEOUT_SECONDS}"
        )
    if record.get("k2") != K2_NOT_APPLICABLE:
        problems.append("the record does not say why K2 is not applicable")
    left = _sequence(record.get("left_running"))
    if left:
        problems.append(
            f"{INVALID}a harness failure: the row left {list(left)} running, and "
            f"the harness ended it"
        )
    calls = [_mapping(call) for call in _sequence(record.get("calls"))]
    first = calls[0] if calls else {}
    if (
        first.get("tool") != WARM_TOOL
        or first.get("outcome") != "returned"
        or first.get("is_error") is not False
        or first.get("read_the_post") is not True
    ):
        problems.append(f"{INVALID}the warm-up read is not recorded as returned")
    forwarded = _mapping(record.get("egress")).get("forwarded")
    if not isinstance(forwarded, list):
        problems.append("the row's egress through its proxy was not recorded")
    elif set(forwarded) - set(ALLOWED_HOSTS):
        problems.append(
            f"the proxy forwarded the row to hosts outside the synthetic origin: "
            f"{sorted(set(forwarded) - set(ALLOWED_HOSTS))}"
        )
    return problems


def _ran(command: Mapping[str, Any], name: str, *, terminal: bool) -> list[str]:
    """Whether *command* is a run the verdict can read: started, ended by
    itself, its output ended, on the kind of stdio its cell needs, and with
    its browser already installed. Every miss is invalid evidence."""
    if not command:
        return [f"{INVALID}the {name} command was never run"]
    found = []
    if command.get("ended_by_harness"):
        found.append(f"{INVALID}the {name} command was ended by the harness")
    if command.get("returncode") is None:
        found.append(f"{INVALID}the {name} command is not shown to exit")
    elif command.get("output_ended") is not True:
        found.append(f"{INVALID}the {name} command's output is not shown to end")
    if command.get("terminal") is not terminal:
        found.append(
            f"{INVALID}the {name} command ran on "
            f"{'pipes' if terminal else 'a terminal'}"
        )
    banner = _first(command, BANNER) is not None
    if terminal and not banner:
        found.append(
            f"{INVALID}the {name} command did not see a terminal: no interactive banner"
        )
    if not terminal and banner:
        found.append(f"{INVALID}the {name} command saw a terminal on pipes")
    if _first(command, INSTALLING) is not None:
        found.append(
            f"{INVALID}the {name} command installed its browser first, which is "
            f"not what is judged"
        )
    return found


def _answered_after(
    command: Mapping[str, Any], prompt: str, index: int, text: str
) -> int | None:
    """When the *index*-th answer, *text*, was typed after *prompt* was seen;
    None when it was not, or before."""
    answers = _answers(command)
    seen = _seen(command, prompt)
    if index >= len(answers) or seen is None:
        return None
    answer = answers[index]
    at = _ns(answer.get("answered_ns"))
    if answer.get("text") != text or at is None or at < seen:
        return None
    return at


# --- H-R10a: the verdict ---------------------------------------------------------


def _deletion_problems(
    record: Mapping[str, Any], logout: Mapping[str, Any]
) -> list[str]:
    if _answered_after(logout, DELETE_PROMPT, 0, "y") is None:
        return [f"{INVALID}the deletion was not confirmed after its prompt was shown"]
    if record.get("authorized") != LOGOUT:
        return [f"{INVALID}the deletion's confirmation was not recorded"]
    return []


def _checkpoint_before_answer(
    record: Mapping[str, Any], logout: Mapping[str, Any], answered: int
) -> list[str]:
    """The owner alive and holding the profile, read after the retirement
    prompt and just before its answer; an owner already gone there retired
    before anyone confirmed, which is a finding when its log says so."""
    point = _mapping(record.get("before_answer"))
    prompt = _seen(logout, RETIRE_PROMPT)
    began, ended = _ns(point.get("began_ns")), _ns(point.get("ended_ns"))
    if began is None or ended is None or prompt is None or began < prompt:
        return [
            f"{INVALID}no checkpoint was read between the retirement prompt and "
            f"its answer"
        ]
    if ended > answered or (answered - ended) / 1e9 > FRESH_SECONDS:
        return [
            f"{INVALID}the checkpoint before the answer is not fresh: it ended "
            f"{(answered - ended) / 1e9:.1f}s before it"
        ]
    if _actor_alive(point) and _lock(point) == HELD:
        return []
    lines = _mapping(record.get("owner_lines"))
    if (
        not _actor_alive(point)
        and lines.get("standing_down")
        and not lines.get("idle_exit")
    ):
        return [
            "the owner retired before the user confirmed: it was gone at the "
            "checkpoint, and its log says a profile command asked and never that "
            "it idled out"
        ]
    return [
        f"{INVALID}the owner was not shown alive and holding the profile right "
        f"before the answer: alive {point.get('actor_alive')!r}, lease "
        f"{_lock(point)!r}"
    ]


def _retired_problems(
    record: Mapping[str, Any], logout: Mapping[str, Any], answered: int
) -> list[str]:
    """The owner retired on the confirmed request, the logout said so after
    the answer, and cleared once the profile was free."""
    found: list[str] = []
    lines = _mapping(record.get("owner_lines"))
    gone = _mapping(record.get("owner_exit"))
    seen = _ns(gone.get("seen_ns"))
    if gone.get("how") != "exited" or seen is None or seen < answered:
        found.append(
            f"the owner is not shown to exit after the confirmed retirement: "
            f"{gone.get('how')!r}"
        )
    if lines.get("idle_exit"):
        found.append(
            f"{INVALID}the owner's log says it idled out, so its exit is not the "
            f"retirement's"
        )
    if lines.get("standing_down") != 1:
        found.append(
            f"the owner's log does not say once that a profile command asked: "
            f"{lines.get('standing_down')!r}"
        )
    retiring = _first(logout, RETIRING_LINE)
    if retiring is None:
        found.append("the logout did not report the owner retiring")
    elif retiring < answered:
        found.append("the logout reported a retirement before the user confirmed it")
    if logout_outcome(logout) != "cleared":
        found.append(
            f"the logout did not clear once the profile was free: "
            f"{logout_outcome(logout)}, exit {logout.get('returncode')!r}"
        )
    cleared = _first(logout, CLEARED_LINE)
    if retiring is not None and cleared is not None and cleared < retiring:
        found.append("the logout cleared before the owner was retiring")
    identified = _identified(record)
    if identified is None or _launches(record, identified) != ([], []):
        found.append("the row launched another owner beside the one that retired")
    return found


def _confirmed_logout(record: Mapping[str, Any], *, daemon: bool) -> list[str]:
    logout = _command(record, "logout")
    found = _ran(logout, "logout", terminal=True)
    if found and not logout:
        return found
    found += _deletion_problems(record, logout)
    if not daemon:
        settlement = _mapping(record.get("settlement"))
        settled = _ns(settlement.get("seen_ns"))
        started = _ns(logout.get("started_ns"))
        if not _settled(settlement):
            found.append(f"{INVALID}the Direct server's profile was not shown settled")
        elif settled is None or started is None or settled > started:
            found.append(f"{INVALID}the settlement was not read before the logout")
        if _first(logout, RETIRE_PROMPT) is not None:
            found.append("a Direct logout asked to retire a shared browser")
        if logout_outcome(logout) != "cleared":
            found.append(
                f"the logout did not clear: {logout_outcome(logout)}, exit "
                f"{logout.get('returncode')!r}"
            )
        return found
    answered = _answered_after(logout, RETIRE_PROMPT, 1, "y")
    if answered is None:
        if _seen(logout, RETIRE_PROMPT) is None:
            found.append(
                "the logout never asked to retire the recorded owner on a terminal"
            )
        else:
            found.append(f"{INVALID}the retirement was not confirmed after its prompt")
        return found
    found += _checkpoint_before_answer(record, logout, answered)
    found += _retired_problems(record, logout, answered)
    return found


def _owner_untouched(record: Mapping[str, Any], label: str) -> list[str]:
    """No retirement was asked of the idle owner, which would have retired
    on one: the same lifetime alive, still holding, its log silent."""
    found = []
    if not _owner_kept(record, label):
        found.append(
            "the owner is not shown kept after the command: alive, the same "
            "lifetime and the only owner started"
        )
    lines = _mapping(record.get("owner_lines"))
    if lines.get("standing_down"):
        found.append(
            "the owner stood down for a profile command: a retirement request "
            "reached it"
        )
    if lines.get("idle_exit"):
        found.append(f"{INVALID}the owner idled out during the cell")
    return found


def _declined(record: Mapping[str, Any]) -> list[str]:
    logout = _command(record, "logout")
    found = _ran(logout, "logout", terminal=True)
    if found and not logout:
        return found
    found += _deletion_problems(record, logout)
    answered = _answered_after(logout, RETIRE_PROMPT, 1, "n")
    if answered is None:
        if _seen(logout, RETIRE_PROMPT) is None:
            found.append(
                "the logout never asked to retire the recorded owner on a terminal"
            )
        else:
            found.append(f"{INVALID}the retirement was not declined after its prompt")
        return found
    found += _checkpoint_before_answer(record, logout, answered)
    if logout_outcome(logout) != "declined":
        found.append(
            f"declining did not end the logout with nothing done and exit 0: "
            f"{logout_outcome(logout)}, exit {logout.get('returncode')!r}"
        )
    for line, what in (
        (RETIRING_LINE, "a retirement"),
        (BUSY_LINE, "a busy owner"),
        (CLEARED_LINE, "a clear"),
    ):
        if _first(logout, line) is not None:
            found.append(f"the declined logout still reported {what}")
    found += _after_command(record, "after_command", require_held=True)
    found += _owner_untouched(record, "owner_after_command")
    return found


def _after_command(
    record: Mapping[str, Any], label: str, *, require_held: bool
) -> list[str]:
    point = _mapping(record.get(label))
    found = []
    if not _actor_alive(point):
        found.append(f"the owner is not shown alive {label.replace('_', ' ')}")
    if require_held and _lock(point) not in (HELD, LEASE_UNOBSERVED):
        found.append(
            f"the owner no longer held the profile {label.replace('_', ' ')}: "
            f"{_lock(point)!r}"
        )
    return found


def _no_terminal(record: Mapping[str, Any]) -> list[str]:
    logout = _command(record, "logout")
    found = _ran(logout, "logout", terminal=False)
    if found and not logout:
        return found
    found += _deletion_problems(record, logout)
    if _first(logout, NEEDS_TERMINAL_LINE.strip()) is None:
        found.append("the logout did not say that retiring needs a terminal")
    if _first(logout, RETIRE_PROMPT) is not None:
        found.append("the logout asked to retire without a terminal")
    if len(_answers(logout)) != 1:
        found.append(f"{INVALID}the row answered more than the deletion prompt")
    for line, what in ((RETIRING_LINE, "a retirement"), (BUSY_LINE, "a busy owner")):
        if _first(logout, line) is not None:
            found.append(f"the logout without a terminal still reported {what}")
    held = held_around(record, "before_command", "after_command")
    if held is True and _actor_alive(record.get("before_command")):
        # The ordinary profile checks against a profile shown held.
        if logout_outcome(logout) != "lease-refused" or logout.get("returncode") == 0:
            found.append(
                f"the logout went on beside a profile shown held: "
                f"{logout_outcome(logout)}, exit {logout.get('returncode')!r}"
            )
    elif held is False:
        found.append(
            f"{INVALID}the profile was not shown held around the run, so the "
            f"ordinary checks' refusal is not asserted"
        )
    found += _after_command(record, "after_command", require_held=False)
    found += _owner_untouched(record, "owner_after_command")
    return found


def h_r10a_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R10a's verdict, any of its three cells: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    row = record.get("row")
    if row not in (ROW_LOGOUT, ROW_DECLINE, ROW_NO_TERMINAL):
        return [f"the record is for row {row!r}, which runs no logout"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if row != ROW_LOGOUT and not daemon:
        return [*problems, f"{row} has no Direct column: {K1_NOT_APPLICABLE['reason']}"]
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    quit_ns = _ns(record.get("host_quit_ns"))
    started = _ns(_command(record, "logout").get("started_ns"))
    if quit_ns is None or started is None or started < quit_ns:
        problems.append(f"{INVALID}the logout did not start after the host quit")
    if row == ROW_LOGOUT:
        problems += _confirmed_logout(record, daemon=daemon)
    elif row == ROW_DECLINE:
        problems += _declined(record)
    else:
        problems += _no_terminal(record)
    return problems


# --- H-R10b: the verdict ---------------------------------------------------------


def _gate(record: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    path = _mapping(_mapping(record.get("held")).get(name)).get("path")
    gates = [
        _mapping(gate)
        for gate in _sequence(record.get("gates"))
        if _mapping(gate).get("path") == path
    ]
    return gates[0] if len(gates) == 1 else {}


def _held_problems(
    record: Mapping[str, Any], name: str, command: Mapping[str, Any]
) -> list[str]:
    """The command answered and ended while its section was held, the read
    still open, and the hold let go by the row before its deadline."""
    found: list[str] = []
    gate = _gate(record, name)
    entered = _ns(gate.get("entered_monotonic_ns"))
    if not gate or entered is None:
        return [f"{INVALID}the {name} section never entered its hold"]
    if gate.get("terminal") == DEADLINE:
        found.append(f"{INVALID}the {name} hold ran out its deadline")
    elif gate.get("terminal") not in (SERVED, PEER_GONE):
        found.append(
            f"{INVALID}the {name} hold recorded no end: {gate.get('terminal')!r}"
        )
    if gate.get("released_by") != RELEASED_BY_ROW:
        found.append(
            f"{INVALID}the {name} hold was released by {gate.get('released_by')!r}"
        )
    released = _ns(gate.get("release_requested_monotonic_ns"))
    exited = _ns(command.get("exited_ns"))
    acted = [
        at for at in (_ns(a.get("answered_ns")) for a in _answers(command)) if at
    ] or [_ns(command.get("started_ns"))]
    first = acted[0]
    if first is None or first < entered:
        found.append(f"{INVALID}the {name} command acted before its section was held")
    if exited is None or released is None or exited > released:
        found.append(
            f"{INVALID}the {name} command did not end while its section was held"
        )
    if command.get("read_open") is not True:
        found.append(f"{INVALID}the read had ended before the {name} command did")
    return found


def _refused_busy(
    record: Mapping[str, Any], name: str, command: Mapping[str, Any]
) -> list[str]:
    """K3: refused on the busy path, exit 1, no process named, nothing done."""
    found: list[str] = []
    outcome = busy_outcome(name, command)
    if outcome != "busy-refused" or command.get("returncode") != 1:
        found.append(
            f"the {name} command beside a busy owner was not refused as busy with "
            f"exit 1: {outcome}, exit {command.get('returncode')!r}"
        )
    answers = _answers(command)
    confirmed = _answered_after(command, RETIRE_PROMPT, len(answers) - 1, "y")
    if confirmed is None:
        found.append(
            f"{INVALID}the {name} retirement was not confirmed after its prompt"
        )
    busy = _first(command, BUSY_LINE)
    if confirmed is not None and busy is not None and busy < confirmed:
        found.append(f"the {name} command reported busy before the user confirmed")
    if _first(command, RETIRING_LINE) is not None:
        found.append(
            f"the {name} command read the busy owner's answer as an accepted retirement"
        )
    if outcome == "proceeded":
        found.append(f"the {name} command changed the profile beside a busy owner")
    identified = _identified(record)
    if identified is not None and str(identified[0]) in _text(command):
        found.append(f"the {name} command's output names the owner's process")
    if name == "import":
        for line, what in (
            (KEYCHAIN_NOTICE, "the keychain notice"),
            (IMPORT_FOUND, "a discovered browser profile"),
            (IMPORT_VALIDATING, "cookies read from a browser"),
        ):
            if _first(command, line) is not None:
                found.append(f"the busy import went on to {what} before refusing")
    return found


def _refused_at_lease(
    record: Mapping[str, Any], name: str, command: Mapping[str, Any]
) -> list[str]:
    """K1 frozen: the Direct's own policy at its lease. A login that waited
    is bounded by the row; nothing may take the profile."""
    found: list[str] = []
    outcome = busy_outcome(name, command)
    if outcome == "proceeded":
        found.append(f"the frozen {name} took the profile while the lease was held")
    elif name == "login":
        if outcome not in ("waited", "lease-refused"):
            found.append(f"the frozen login neither waited nor refused: {outcome}")
    elif outcome != "lease-refused" or command.get("returncode") == 0:
        found.append(
            f"the frozen {name} was not refused at the lease: {outcome}, exit "
            f"{command.get('returncode')!r}"
        )
    if name == "import" and _first(command, IMPORT_VALIDATING) is not None:
        found.append("the frozen import read a browser's cookies past its lease")
    return found


def h_r10b_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R10b's verdict over its raw record: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    if record.get("row") != ROW_BUSY:
        return [f"the record is for row {record.get('row')!r}, which holds no read"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if record.get("username") != BUSY_USERNAME:
        problems.append(f"{INVALID}the record names {record.get('username')!r}")
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    for name, _, _ in BUSY_COMMANDS:
        command = _command(record, name)
        problems += _ran(command, name, terminal=True)
        if not command:
            continue
        problems += _held_problems(record, name, command)
        if name == "logout" and _answered_after(command, DELETE_PROMPT, 0, "y") is None:
            problems.append(f"{INVALID}the deletion was not confirmed after its prompt")
        if daemon:
            problems += _refused_busy(record, name, command)
        else:
            problems += _refused_at_lease(record, name, command)
    reads = _calls(record, PERSON_TOOL)
    read = reads[0] if len(reads) == 1 else None
    if read is None:
        problems.append(f"{INVALID}the record holds {len(reads)} held reads, not one")
    elif record.get("read_open") or not _read_ok(read):
        problems.append(
            f"the held read did not complete normally after its release: outcome "
            f"{read.get('outcome')!r}, error {read.get('is_error')!r}"
        )
    else:
        problems += [
            f"held read: {p}" for p in _page_problems(record, BUSY_USERNAME, read)
        ]
        last = _ns(_command(record, "import").get("release_requested_ns"))
        if last is not None and (_ns(read.get("ended_monotonic_ns")) or 0) < last:
            problems.append(f"{INVALID}the read ended before its last release")
    if daemon:
        problems += _owner_untouched(record, "owner_after_read")
    problems += _session_problems(record, [BUSY_USERNAME])
    return problems


# --- H-R15: the verdict ----------------------------------------------------------


def _held_for_status(record: Mapping[str, Any], *, daemon: bool) -> bool | None:
    """Whether the profile was shown held around the status run: by the
    contender where it answers, else by the browser root on the profile at
    both readings; the owner alive at both in daemon mode."""
    held = held_around(record, "before_status", "after_status")
    if held is None:
        before, after = (
            _roots(record.get("roots_before")),
            _roots(record.get("roots_after")),
        )
        held = bool(before) and bool(after)
    if daemon:
        held = held and all(
            _actor_alive(record.get(label))
            for label in ("before_status", "after_status")
        )
    return held


def h_r15_problems(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """H-R15's verdict over its raw record: every problem, or nothing."""
    if not isinstance(record, Mapping):
        return ["the row kept no record"]
    if record.get("row") != ROW_STATUS:
        return [f"the record is for row {record.get('row')!r}, which runs no status"]
    problems = _common(record, daemon=daemon)
    problems += host_problems(record.get("host"))
    if daemon and _identified(record) is None:
        return [*problems, f"{INVALID}the owner was never identified"]
    status = _command(record, "status")
    ran = _ran(status, "status", terminal=False)
    problems += ran
    if not status:
        return problems
    category = status_category(status)
    held = _held_for_status(record, daemon=daemon)
    for label in ("before_status", "after_status"):
        point = _mapping(record.get(label))
        if _ns(point.get("ended_ns")) is None:
            problems.append(f"{INVALID}no checkpoint {label.replace('_', ' ')}")
    started, exited = _ns(status.get("started_ns")), _ns(status.get("exited_ns"))
    before_end = _ns(_mapping(record.get("before_status")).get("ended_ns"))
    after_began = _ns(_mapping(record.get("after_status")).get("began_ns"))
    if (
        started is None
        or exited is None
        or before_end is None
        or after_began is None
        or before_end > started
        or exited > after_began
    ):
        problems.append(f"{INVALID}the checkpoints do not bracket the status run")
    if category == VALID:
        if held:
            problems.append(
                "the status checked the session while the profile was shown held "
                "around it"
            )
        else:
            problems.append(
                f"{INVALID}a timing branch: the profile was not shown held around "
                f"the status, which then succeeded; no contention is claimed"
            )
    elif category in (NO_SESSION, BRIDGE):
        problems.append(
            f"{INVALID}the status never reached the profile: {category}, a staging "
            f"problem"
        )
    elif category != CONTENTION:
        problems.append(
            f"the status did not end in contention: {category}, exit "
            f"{status.get('returncode')!r}"
        )
    else:
        if status.get("returncode") != 1:
            problems.append(
                f"the contended status exited {status.get('returncode')!r}, not 1"
            )
        if not held:
            problems.append(
                f"{INVALID}the profile was not shown held around the status, so "
                f"its contention is not proved"
            )
    if daemon and category == CONTENTION:
        text = _text(status)
        if STATUS_HELD not in text:
            problems.append("the status did not say another process holds the profile")
        if STATUS_SHARED.strip() not in text:
            problems.append("the status did not name the recorded shared browser")
    if started is not None and exited is not None:
        during = [
            _mapping(r).get("path")
            for r in _sequence(record.get("requests"))
            if (at := _ns(_mapping(r).get("monotonic_ns"))) is not None
            and started <= at <= exited
        ]
        if during:
            problems.append(
                f"the origin was asked for pages during the status: {during}"
            )
    before, after = (
        _roots(record.get("roots_before")),
        _roots(record.get("roots_after")),
    )
    if before is None or after is None:
        problems.append(f"{INVALID}the browser roots were not read around the status")
    elif len(after) > len(before) or not all(
        any(same_lifetime(root, kept) for kept in before) for root in after
    ):
        problems.append(
            f"a browser was launched by the status: roots {before} before, {after} after"
        )
    reads = _calls(record, WARM_TOOL)
    again = reads[1] if len(reads) == 2 else None
    if again is None:
        problems.append(f"{INVALID}the record holds {len(reads)} feed reads, not two")
    else:
        if (
            again.get("outcome") != "returned"
            or again.get("is_error") is not False
            or again.get("read_the_post") is not True
        ):
            problems.append("the host's read after the status did not return the post")
        if exited is not None and (_ns(again.get("began_monotonic_ns")) or 0) < exited:
            problems.append(
                f"{INVALID}the second read was sent before the status ended"
            )
    if daemon:
        if not _owner_kept(record, "owner_after_read"):
            problems.append(
                "the owner is not shown kept after the second read: alive, the "
                "same lifetime and the only owner started"
            )
    return problems


# --- The verdicts, by row --------------------------------------------------------


def problems_for(record: Mapping[str, Any] | None, *, daemon: bool) -> list[str]:
    """The verdict of whichever row of this module *record* is for."""
    row = _mapping(record).get("row")
    if row == ROW_BUSY:
        return h_r10b_problems(record, daemon=daemon)
    if row == ROW_STATUS:
        return h_r15_problems(record, daemon=daemon)
    return h_r10a_problems(record, daemon=daemon)


def invalid_evidence(problems: Sequence[str]) -> list[str]:
    """The problems that leave a row unmeasured rather than failed."""
    return [problem for problem in problems if problem.startswith(INVALID)]


def semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """What K0 compares: classifications only, no pid, time or path."""
    row = record.get("row")
    found: dict[str, Any] = {"row": row, "mode": record.get("mode")}
    if row == ROW_STATUS:
        status = _command(record, "status")
        found["status"] = status_category(status)
        found["exit"] = status.get("returncode")
        return found
    if row == ROW_BUSY:
        for name, _, _ in BUSY_COMMANDS:
            command = _command(record, name)
            found[name] = busy_outcome(name, command)
        found["owner_kept"] = _owner_kept(record, "owner_after_read")
        return found
    logout = _command(record, "logout")
    found["logout"] = logout_outcome(logout)
    found["exit"] = logout.get("returncode")
    found["asked_to_retire"] = _first(logout, RETIRE_PROMPT) is not None
    found["retired"] = _mapping(record.get("owner_exit")).get("how") == "exited"
    return found


def _refusals(named: Sequence[tuple[str, Mapping[str, Any] | None, bool]]) -> list[str]:
    refusals = []
    for name, record, daemon in named:
        problems = problems_for(record, daemon=daemon)
        if problems:
            refusals.append(f"the {name} record is not valid: {problems}")
    return refusals


def semantic_differences(
    reference: Mapping[str, Any] | None, repeat: Mapping[str, Any] | None
) -> list[str]:
    """K0 against K3: both valid by their own verdict, and alike in every
    classification. A missing or invalid record is a refusal."""
    refusals = _refusals([("reference", reference, True), ("repeat", repeat, True)])
    if refusals:
        return refusals
    assert reference is not None and repeat is not None
    one, two = semantics(reference), semantics(repeat)
    return [
        f"{name}: {one[name]!r} then {two.get(name)!r}"
        for name in one
        if one[name] != two.get(name)
    ]


def comparison_refusals(
    direct: Mapping[str, Any] | None, daemon: Mapping[str, Any] | None
) -> list[str]:
    """Why K3 cannot be held to K1 on a row here: a record missing or invalid.
    O1 to O4 are the vectors' (``compare_to_direct``); the commands' own
    outcomes differ by design, which K2's record explains."""
    return _refusals([("Direct", direct, False), ("daemon", daemon, True)])


# --- What stays with the models

MODEL_COVERAGE = model_coverage.MODEL_COVERAGE
