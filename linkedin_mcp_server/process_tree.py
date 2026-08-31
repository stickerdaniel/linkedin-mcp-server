"""Contain subprocess descendants behind one platform-sized boundary."""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import logging
import os
import secrets
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"
_adopted_windows_job: int | None = None
#: The gate that launched this owner, which is a member of the same Job and must
#: survive every drain below. It spawns the owner and waits, so it is the process
#: the frontend reads an exit status from, and a drain that ends it replaces that
#: status with the termination code. Recorded while it is provably the parent and
#: provably alive, which is what makes the id safe to hold: Windows cannot reuse
#: it while the gate is still waiting on this process.
_adopted_windows_gate: int | None = None
_retained_windows_jobs: list[WindowsJob] = []
_BROWSER_PROCESS_MARKER = "LINKEDIN_MCP_BROWSER_PROCESS_MARKER"


@dataclass
class _PosixGroupRegistration:
    leader_identity: str | None
    members: dict[int, str]
    markers: set[str] = field(default_factory=set)
    #: The markers a process in this group was seen *carrying*, rather than the
    #: ones inferred from ancestry. A hard exit kills either kind, because by
    #: then nothing this owner started may outlive it. One browser's close may
    #: not: the installer supervisor is detached too, so it lands in ``markers``
    #: for whichever launch happened to look while it ran, and killing it there
    #: would end a download nobody asked to stop.
    proved_markers: set[str] = field(default_factory=set)


_registered_posix_groups: dict[int, _PosixGroupRegistration] = {}
_registered_browser_markers: set[str] = set()
_browser_guardian_process: subprocess.Popen[Any] | None = None
_browser_guardian_control_fd: int | None = None
_live_windows_jobs: list[WindowsJob] = []
_JOB_API_FAILURE_LIMIT = 3
_JOB_POLL_SECONDS = 0.01
_JOB_NAME_ATTEMPTS = 8
_RELEASE_NONCE_BYTES = 32
_POSIX_PROCESS_SNAPSHOT_SECONDS = 1.0
#: How long one browser close may spend proving its own launch is gone. Long
#: enough for a wedged Chromium to answer SIGKILL, short enough that a close
#: which cannot prove it still returns and lets the caller keep the profile.
_MARKER_DRAIN_SECONDS = 10.0
#: The kernel's own word for "this process has exited and only its unreaped
#: table entry is left". Nothing else in a process row says it: PID, PGID and
#: the start timestamp all survive the exit unchanged, which is exactly why the
#: identity checks in this module cannot see it on their own.
_ZOMBIE_STATE = "Z"
#: How often a settling process group is asked for its members' run states.
#: The group-exists poll it guards runs ten times as often because it costs one
#: signal, while a run-state answer costs a whole process snapshot: a procfs
#: walk on Linux and a ``ps`` fork everywhere else.
_GROUP_RUN_STATE_SECONDS = 0.1

#: parent, process group, kernel start identity, kernel run state.
_ProcessRow = tuple[int, int, str | None, str | None]


class ProcessTreeError(RuntimeError):
    """The operating system could not establish descendant containment."""


def start_browser_guardian(lease_fd: int) -> None:
    """Start a detached POSIX process that retains the profile lease on crashes."""
    global _browser_guardian_control_fd, _browser_guardian_process
    if _IS_WINDOWS:
        return
    if _browser_guardian_process is not None:
        if _browser_guardian_process.poll() is None:
            return
        _browser_guardian_process = None
        _browser_guardian_control_fd = None

    control_read, control_write = os.pipe()
    ready_read, ready_write = os.pipe()
    owner_pid = os.getpid()
    owner_group = os.getpgrp()
    # Zero unless this process leads its own group, because the group it sits in
    # otherwise belongs to whoever started it: a shell, an MCP client, a CI
    # runner. Killing that on a crash would take processes this server never
    # launched. The cost is that the guardian then ends only the marked Chromium
    # groups, so an unmarked survivor of the same crash, most likely the Node
    # driver, is left to exit on its own. That one holds no profile once its
    # Chromium is gone, which is why the trade goes this way.
    protected_owner_group = owner_group if owner_group == owner_pid else 0
    guardian = Path(__file__).with_name("process_guardian.py").resolve()
    process: subprocess.Popen[Any] | None = None
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-u",
                str(guardian),
                str(control_read),
                str(ready_write),
                str(protected_owner_group),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(control_read, ready_write, lease_fd),
            start_new_session=True,
        )
        os.close(control_read)
        control_read = -1
        os.close(ready_write)
        ready_write = -1
        readable, _, _ = select.select([ready_read], [], [], 5.0)
        if not readable or os.read(ready_read, 6) != b"ready\n":
            raise ProcessTreeError("The browser crash guardian did not start")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(control_write)
        if process is not None:
            with contextlib.suppress(OSError):
                process.kill()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=5)
        raise
    finally:
        for descriptor in (control_read, ready_read, ready_write):
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)

    _browser_guardian_process = process
    _browser_guardian_control_fd = control_write


def release_browser_guardian() -> None:
    """Release the crash guardian after Chromium is confirmed gone."""
    global _browser_guardian_control_fd, _browser_guardian_process
    process = _browser_guardian_process
    control_fd = _browser_guardian_control_fd
    _browser_guardian_process = None
    _browser_guardian_control_fd = None
    if control_fd is not None:
        with contextlib.suppress(OSError):
            os.write(control_fd, b"release\n")
        with contextlib.suppress(OSError):
            os.close(control_fd)
    if process is None:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.wait(timeout=5)


def _send_marker_to_guardian(marker: str) -> None:
    process = _browser_guardian_process
    control_fd = _browser_guardian_control_fd
    if process is None or control_fd is None:
        return
    if process.poll() is not None:
        raise ProcessTreeError("The browser crash guardian exited unexpectedly")
    try:
        os.write(control_fd, f"marker {marker}\n".encode("ascii"))
    except OSError as exc:
        raise ProcessTreeError(
            "The browser crash guardian could not retain its marker"
        ) from exc


def new_browser_process_marker() -> tuple[str, dict[str, str]]:
    """Return a private marker and the browser environment entry that carries it."""
    marker = secrets.token_hex(32)
    _send_marker_to_guardian(marker)
    return marker, {_BROWSER_PROCESS_MARKER: marker}


def _stat_fields(pid: int) -> list[str]:
    """The procfs ``stat`` fields of *pid*, from its run state onwards."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except (OSError, UnicodeError):
        return []
    closing = raw.rfind(")")
    return raw[closing + 2 :].split() if closing >= 0 else []


def _linux_process_rows() -> dict[int, _ProcessRow]:
    """Read ancestry, groups, kernel start identities and run states from procfs."""
    rows: dict[int, _ProcessRow] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
        except (OSError, UnicodeError):
            continue
        closing = raw.rfind(")")
        fields = raw[closing + 2 :].split() if closing >= 0 else []
        if len(fields) <= 19:
            continue
        try:
            process = int(entry.name)
            parent = int(fields[1])
            group = int(fields[2])
        except ValueError:
            continue
        rows[process] = (parent, group, f"proc:{fields[19]}", fields[0])
    return rows


def _ps_process_rows() -> dict[int, _ProcessRow]:
    """Read process ancestry on POSIX systems without procfs."""
    ps = next(
        (
            candidate
            for candidate in ("/bin/ps", "/usr/bin/ps")
            if Path(candidate).is_file()
        ),
        None,
    )
    if ps is None:
        return {}
    try:
        snapshot = subprocess.run(
            [ps, "-A", "-o", "pid=", "-o", "ppid=", "-o", "pgid=", "-o", "state="],
            check=True,
            capture_output=True,
            text=True,
            timeout=_POSIX_PROCESS_SNAPSHOT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}

    rows: dict[int, _ProcessRow] = {}
    for line in snapshot.splitlines():
        fields = line.split()
        # BSD ``state`` is a run-state letter plus optional flag letters
        # (``Ss``, ``S+``), so only its first character is the state itself.
        # A row without one is kept rather than dropped: the state is an extra
        # signal here and never the only one.
        if len(fields) == 3:
            fields = [*fields, ""]
        if len(fields) != 4:
            continue
        try:
            process, parent, group = map(int, fields[:3])
        except ValueError:
            continue
        rows[process] = (parent, group, None, fields[3][:1] or None)
    return rows


def _posix_process_rows() -> dict[int, _ProcessRow]:
    return (
        _linux_process_rows()
        if sys.platform.startswith("linux")
        else _ps_process_rows()
    )


def _has_exited_unreaped(pid: int, state: str | None) -> bool:
    """Whether nothing is left of *pid* but an unreaped process-table entry.

    A zombie has already released every file, mapping and lock it held, so it
    cannot touch the profile, and it cannot be waited for by this owner when its
    parent is somebody else -- a Chromium grandchild reparented to PID 1 in a
    container stays a zombie for as long as that PID 1 declines to reap. Without
    this the hard-exit drain has nothing to end its loop on: ``/proc`` still
    reports the PGID and the start time, so every identity check below keeps
    answering that the group is alive, and the owner never kills its own group
    or releases its locks.

    Only the kernel run state answers it, and only where a run state exists.
    Darwin needs no second answer: ``proc_pidinfo(PROC_PIDTBSDINFO)`` reads zero
    bytes for a zombie, so :func:`_kernel_start_identity` already returns None
    there and the identity comparison rejects it (measured on 26.0).
    """
    if state is None and sys.platform.startswith("linux"):
        fields = _stat_fields(pid)
        state = fields[0] if fields else None
    return state == _ZOMBIE_STATE


class _MarkerScan(NamedTuple):
    """One look for the processes carrying a browser launch marker.

    ``conclusive`` is what separates "nothing carries this marker" from "the
    scan could not tell". Outside Linux the scan shells out to ``ps``, and a
    missing, hung or failing ``ps`` yields the same empty output as a browser
    that has already gone. Reading emptiness as proof there would let a close
    report a drained launch it never managed to look at, so every consumer that
    turns this into a verdict checks the flag first.

    Linux answers from ``/proc`` and is always conclusive: an unreadable
    ``environ`` belongs to a process that exited or to another user, neither of
    which is this launch's Chromium.
    """

    processes: tuple[int, ...]
    conclusive: bool


def _scan_marked_posix_processes(marker: str) -> _MarkerScan:
    """Scan for processes carrying this browser launch's private environment marker."""
    expected = f"{_BROWSER_PROCESS_MARKER}={marker}".encode()
    if sys.platform.startswith("linux"):
        found: list[int] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if expected in environment:
                found.append(int(entry.name))
        return _MarkerScan(tuple(found), True)

    ps = next(
        (
            candidate
            for candidate in ("/bin/ps", "/usr/bin/ps")
            if Path(candidate).is_file()
        ),
        None,
    )
    if ps is None:
        # Debug, like the failure below it. A drain polls this every
        # ``_JOB_POLL_SECONDS`` and scans twice per round, so a machine with no
        # usable ``ps`` would bury its one actionable line under a thousand
        # copies of this one. The drain says it once, at its deadline.
        logger.debug(
            "No ps executable was found, so browser processes cannot be "
            "identified by their launch marker on this platform."
        )
        return _MarkerScan((), False)
    try:
        snapshot = subprocess.run(
            [ps, "eww", "-A", "-o", "pid=", "-o", "command="],
            check=True,
            capture_output=True,
            timeout=_POSIX_PROCESS_SNAPSHOT_SECONDS,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug(
            "The process snapshot for the browser launch marker failed (%s: %s), "
            "so nothing can be concluded from it.",
            type(exc).__name__,
            exc,
        )
        return _MarkerScan((), False)

    found = []
    needle = expected + b" "
    for line in snapshot.splitlines():
        fields = line.lstrip().split(maxsplit=1)
        if len(fields) != 2 or needle not in fields[1] + b" ":
            continue
        try:
            found.append(int(fields[0]))
        except ValueError:
            continue
    return _MarkerScan(tuple(found), True)


def _posix_detached_descendants(
    pid: int, process_group: int
) -> tuple[tuple[int, int, str], ...]:
    """Snapshot escaped descendants, groups and kernel start identities."""
    try:
        rows = _posix_process_rows()
    except OSError:
        return ()
    children: dict[int, list[int]] = {}
    for process, (parent, _group, _identity, _state) in rows.items():
        children.setdefault(parent, []).append(process)

    detached: list[tuple[int, int, int, str]] = []
    pending = [(pid, 0)]
    seen = {pid}
    while pending:
        parent, depth = pending.pop()
        for child in children.get(parent, ()):
            if child in seen:
                continue
            seen.add(child)
            pending.append((child, depth + 1))
            _parent, group, identity, _state = rows[child]
            if group == process_group:
                continue
            identity = identity or _kernel_start_identity(child)
            if identity is not None:
                detached.append((depth + 1, child, group, identity))
    return tuple(
        (child, group, identity)
        for _depth, child, group, identity in sorted(detached, reverse=True)
    )


def remember_detached_process_groups(marker: str | None = None) -> None:
    """Retain browser groups by ancestry and a launch-specific process marker."""
    if os.name == "nt":
        return
    pid = os.getpid()
    process_group = os.getpgrp()
    rows: dict[int, _ProcessRow]
    try:
        rows = _posix_process_rows()
    except OSError:
        rows = {}

    observed = list(_posix_detached_descendants(pid, process_group))
    guardian = _browser_guardian_process
    if guardian is not None:
        observed = [
            item
            for item in observed
            if item[0] != guardian.pid and item[1] != guardian.pid
        ]
    marked: set[int] = set()
    if marker is not None:
        _registered_browser_markers.add(marker)
        known = {process for process, _group, _identity in observed}
        # An inconclusive scan contributes nothing to ``marked``, so the
        # registration below records no proved marker. That is the closed
        # direction: a single close may then not kill this group, and the drain
        # that ends the close cannot claim it is empty either.
        for process in _scan_marked_posix_processes(marker).processes:
            marked.add(process)
            if process in known:
                continue
            row = rows.get(process)
            if row is None or row[1] == process_group:
                continue
            identity = row[2] or _kernel_start_identity(process)
            if identity is not None:
                observed.append((process, row[1], identity))

    grouped: dict[int, dict[int, str]] = {}
    for process, group, identity in observed:
        grouped.setdefault(group, {})[process] = identity
    for group, members in grouped.items():
        _registered_posix_groups[group] = _PosixGroupRegistration(
            leader_identity=_kernel_start_identity(group),
            members=members,
            markers={marker} if marker is not None else set(),
            # Ancestry put every detached descendant in this registration, and
            # only some of them are this browser. Which is which decides what a
            # single close may kill; see :class:`_PosixGroupRegistration`.
            proved_markers=(
                {marker} if marker is not None and marked & members.keys() else set()
            ),
        )


def forget_browser_process_marker(marker: str) -> None:
    """Drop confirmed-gone browser registrations from future hard exits."""
    _registered_browser_markers.discard(marker)
    for group, registration in tuple(_registered_posix_groups.items()):
        if marker not in registration.markers:
            continue
        registration.markers.discard(marker)
        registration.proved_markers.discard(marker)
        if not registration.markers:
            _registered_posix_groups.pop(group, None)


def _refresh_marked_process_groups(
    rows: dict[int, _ProcessRow],
    markers: tuple[str, ...] | None = None,
) -> None:
    """Add every currently marked browser group to the hard-exit registry.

    *markers* narrows the scan to one launch. A hard exit passes nothing and
    takes every marker it knows; a single browser's close passes its own, so it
    cannot adopt a sibling launch's group on the way past.
    """
    owner_group = os.getpgrp()
    grouped: dict[int, dict[int, str]] = {}
    grouped_markers: dict[int, set[str]] = {}
    scanned = tuple(_registered_browser_markers) if markers is None else markers
    for marker in scanned:
        # Only what a scan actually saw is added. An inconclusive one adds no
        # group, which costs nothing here: the registrations already filed keep
        # being checked against the process rows, and the proof that ends a
        # drain is taken from its own scan rather than from this registry.
        for process in _scan_marked_posix_processes(marker).processes:
            row = rows.get(process)
            if row is not None:
                group = row[1]
                identity = row[2] or _kernel_start_identity(process)
            else:
                try:
                    group = os.getpgid(process)
                except OSError:
                    continue
                identity = _kernel_start_identity(process)
            if group == owner_group or identity is None:
                continue
            grouped.setdefault(group, {})[process] = identity
            grouped_markers.setdefault(group, set()).add(marker)

    for group, members in grouped.items():
        registration = _registered_posix_groups.get(group)
        if registration is None:
            _registered_posix_groups[group] = _PosixGroupRegistration(
                leader_identity=_kernel_start_identity(group),
                members=members,
                markers=grouped_markers[group],
                # Every marker here came off a live process environment, which
                # is the observation a single close is allowed to act on.
                proved_markers=set(grouped_markers[group]),
            )
        else:
            registration.members.update(members)
            registration.markers.update(grouped_markers[group])
            registration.proved_markers.update(grouped_markers[group])


def _registered_group_still_matches(
    group: int,
    registration: _PosixGroupRegistration,
    rows: dict[int, _ProcessRow],
) -> bool:
    """Whether this group still holds a live process of the launch it was filed under.

    Both anchors are checked against the kernel run state as well as the start
    identity, and only a zombie is discounted. That is deliberately the one
    exception: a zombie has released everything it held, so it can neither open
    the profile nor be reaped by this owner, while every other state -- stopped,
    uninterruptible, traced -- is a process that can still come back and must
    keep the locks. The identity comparison is untouched, so a reused PID or
    PGID is still refused (#809).
    """
    leader = rows.get(group)
    if (
        leader is not None
        and leader[1] == group
        and not _has_exited_unreaped(group, leader[3])
    ):
        current = leader[2] or _kernel_start_identity(group)
        if (
            current is not None
            and registration.leader_identity is not None
            and current == registration.leader_identity
        ):
            return True
    if not process_group_exists(group):
        return False
    for process, identity in registration.members.items():
        row = rows.get(process)
        if row is None:
            try:
                if os.getpgid(process) != group:
                    continue
            except OSError:
                continue
            if _has_exited_unreaped(process, None):
                continue
            current = _kernel_start_identity(process)
        else:
            if row[1] != group:
                continue
            if _has_exited_unreaped(process, row[3]):
                continue
            current = row[2] or _kernel_start_identity(process)
        if current is not None and current == identity:
            return True
    return False


def _kill_registered_process_groups() -> tuple[int, ...]:
    try:
        rows = _posix_process_rows()
    except OSError:
        rows = {}
    _refresh_marked_process_groups(rows)

    targeted: list[int] = []
    for group, registration in tuple(_registered_posix_groups.items()):
        if not _registered_group_still_matches(group, registration, rows):
            continue
        targeted.append(group)
        with contextlib.suppress(OSError):
            os.killpg(group, signal.SIGKILL)
    return tuple(targeted)


def _wait_for_process_groups(
    groups: tuple[int, ...],
    *,
    markers: tuple[str, ...] | None = None,
    deadline: float | None = None,
) -> bool:
    """Keep the owner's locks until every targeted browser group is gone.

    Returns whether they went. A hard exit passes no *deadline* and waits as
    long as it takes, because it holds the daemon and profile locks while it
    does. A single browser's close passes one: it has to return either way, and
    an unproven drain is reported rather than waited out.
    """
    remaining = set(groups)
    while remaining:
        for group in tuple(remaining):
            registration = _registered_posix_groups.get(group)
            if registration is None:
                continue
            for process in tuple(registration.members):
                with contextlib.suppress(ChildProcessError, OSError):
                    os.waitpid(process, os.WNOHANG)
        try:
            rows = _posix_process_rows()
        except OSError:
            rows = {}
        _refresh_marked_process_groups(rows, markers)
        remaining = {
            group
            for group in remaining
            if (
                (registration := _registered_posix_groups.get(group)) is not None
                and _registered_group_still_matches(group, registration, rows)
            )
        }
        if remaining:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(_JOB_POLL_SECONDS)
    return True


def _kill_marked_process_groups(marker: str) -> tuple[int, ...]:
    """Kill the groups this one launch marker is known to account for.

    Narrower than the hard-exit sweep in two ways, because this runs while the
    owner keeps living. Only groups whose marker was read off a process
    environment are targeted, so a detached installer that ancestry happened to
    file under this launch is left alone, and the owner's own group is never a
    candidate however it got registered.
    """
    try:
        rows = _posix_process_rows()
    except OSError:
        rows = {}
    _refresh_marked_process_groups(rows, (marker,))

    owner_group = os.getpgrp()
    targeted: list[int] = []
    for group, registration in tuple(_registered_posix_groups.items()):
        if group == owner_group or marker not in registration.proved_markers:
            continue
        if not _registered_group_still_matches(group, registration, rows):
            continue
        targeted.append(group)
        with contextlib.suppress(OSError):
            os.killpg(group, signal.SIGKILL)
    return tuple(targeted)


def _drain_marked_posix_groups(marker: str, deadline: float) -> bool:
    """Kill and bury every POSIX group carrying one browser launch marker.

    The scan that supplies the proof can fail to run at all, and it reports an
    empty machine when it does. Only a scan that answered may end this, so a
    ``ps`` that is missing, hung or broken keeps the loop going and then reports
    an unproven drain rather than a clean one.
    """
    while True:
        groups = _kill_marked_process_groups(marker)
        if groups and not _wait_for_process_groups(
            groups, markers=(marker,), deadline=deadline
        ):
            return False
        # The kill above works on process groups, so it cannot reach a marked
        # process sharing the owner's own group. This is the proof rather than
        # the action: the drain is complete only once nothing carries the
        # marker, whichever group it sat in.
        scan = _scan_marked_posix_processes(marker)
        if scan.conclusive and not scan.processes:
            return True
        if time.monotonic() >= deadline:
            if not scan.conclusive:
                logger.error(
                    "The browser launch marker could not be scanned for, so "
                    "this shutdown stays unproven rather than assumed clean."
                )
            return False
        time.sleep(_JOB_POLL_SECONDS)


def _drain_exclusions() -> frozenset[int]:
    """Process ids no adopted-Job drain may end.

    This owner, which has to survive its own browser, and the gate that launched
    it, which is in the same Job because that is how a frontend assigns the Job
    before the owner exists. The gate then waits and mirrors the owner's exit
    status, so ending it costs the frontend that status and says nothing about
    the browser the drain was aimed at.
    """
    spared = {0, os.getpid()}
    if _adopted_windows_gate is not None:
        spared.add(_adopted_windows_gate)
    return frozenset(spared)


def _drain_adopted_windows_job() -> None:
    """Terminate every other Job member before this owner releases its locks."""
    if _adopted_windows_job is None:
        return
    win32api, win32con, win32job, _winerror = _windows_modules()
    spared = _drain_exclusions()
    while True:
        try:
            members = win32job.QueryInformationJobObject(
                _adopted_windows_job, win32job.JobObjectBasicProcessIdList
            )
        except BaseException:  # noqa: BLE001 - releasing the locks is less safe
            time.sleep(_JOB_POLL_SECONDS)
            continue
        descendants = tuple(
            int(process)
            for process in members
            if process is not None and int(process) not in spared
        )
        if not descendants:
            return
        for process in descendants:
            handle: Any | None = None
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_TERMINATE
                    | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                    False,
                    process,
                )
                if win32job.IsProcessInJob(handle, _adopted_windows_job):
                    win32api.TerminateProcess(handle, 1)
            except BaseException:  # noqa: BLE001 - the next Job query proves exit
                pass
            finally:
                if handle is not None:
                    with contextlib.suppress(BaseException):
                        handle.Close()
        time.sleep(_JOB_POLL_SECONDS)


def _patchright_driver_process(playwright: Any) -> Any:
    """The Node driver process behind one Patchright ``Playwright`` handle.

    Measured against patchright 1.61.2 (``_impl/_transport.py``): the async
    driver is started by ``asyncio.create_subprocess_exec`` inside
    ``PipeTransport.connect`` and kept on the transport as ``_proc``, reached
    from the public object through ``_impl_obj._connection._transport``. Private
    the whole way, so ``tests/test_process_tree.py`` pins the shape against a
    real driver rather than against a hand-written double.

    Raises rather than returning None on anything unexpected. The caller uses
    this to build the only attribution Windows has, and a launch it cannot
    attribute must not start.
    """
    impl = getattr(playwright, "_impl_obj", playwright)
    connection = getattr(impl, "_connection", None)
    transport = getattr(connection, "_transport", None)
    process = getattr(transport, "_proc", None)
    if process is None:
        raise ProcessTreeError("Patchright exposed no driver process to contain")
    return process


def contain_browser_launch(playwright: Any) -> WindowsJob | None:
    """Contain one browser launch's descendants behind a Job of its own.

    Windows only; POSIX answers with the environment marker instead and gets
    None. Called after the driver starts and before it is asked to launch
    anything, which is the whole window in which this works: Job membership is
    inherited at creation, so the Node driver has to be in the Job before it
    spawns Chromium, and it spawns nothing until ``launch_persistent_context``.

    This exists because a Job is the *only* attribution Windows has. An
    environment block belongs to its own process and reading another one's takes
    the debugger APIs, so the POSIX marker scan has no counterpart here. Before
    this, the drain answered from the daemon owner's adopted Job and returned
    "empty" whenever there was none -- which is every direct-mode server, and
    direct mode launches browsers too.

    The Job is created kill-on-close and its handle is not inheritable, so a
    crash of this process ends the browser rather than leaving it on the profile.
    """
    if not _IS_WINDOWS:
        return None
    process = _patchright_driver_process(playwright)
    job = WindowsJob.anonymous()
    try:
        job.assign_asyncio_process(process)
    except BaseException:
        # Nothing is in the Job yet, so closing it kills nothing. The handle
        # would otherwise be leaked by a launch that goes on to fail.
        job.close()
        raise
    return job


def _drain_windows_browser_job(job: WindowsJob | None, deadline: float) -> bool:
    """End and bury one launch's Windows Job, and say whether it emptied.

    ``TerminateJobObject`` reaches every process in the Job and nothing outside
    it, which is what keeps this off the installer: its supervisor and worker
    sit in a Job of their own (``WindowsJob.anonymous`` in ``bootstrap``) that
    this handle has no authority over.

    A Job that cannot be emptied keeps its handle, so the containment stays
    armed and this process's exit still ends what is inside it.
    """
    if job is None:
        # The launch produced no Job, so nothing here can name its processes.
        # Not knowing is not proof, and a drain that answers "empty" from an
        # absent Job is the false proof this function replaced.
        logger.error(
            "This browser launch has no Windows Job, so its shutdown cannot be proved."
        )
        return False
    if job.closed:
        # Already settled by an earlier close, whose verdict stands: a proved
        # drain emptied the Job before the handle went, and an abandoned one
        # never proved anything.
        return job.drained
    try:
        job.terminate()
        job.wait_until_empty(timeout=max(deadline - time.monotonic(), 0.0))
    except ProcessTreeError as exc:
        logger.error("The browser Job did not drain: %s", exc)
        return False
    return True


def _in_another_owned_job(win32job: Any, handle: Any) -> bool:
    """Whether an adopted-Job member also sits in a Job this owner still holds."""
    for job in tuple(_live_windows_jobs):
        job_handle = job.job_handle
        if job_handle is None:
            continue
        try:
            if win32job.IsProcessInJob(handle, job_handle):
                return True
        except BaseException:  # noqa: BLE001 - an unanswered Job proves nothing
            continue
    return False


def _drain_adopted_windows_job_members(deadline: float) -> bool:
    """Prove the adopted Job holds nobody but this owner, ending what is left.

    Windows has no marker to scan for: an environment block belongs to its own
    process, and reading another one's takes the debugger APIs. The Job is the
    whole of the attribution there, so this drains what the Job still holds,
    minus the exclusions that keep it from being a tree kill. The owner and the
    gate that launched it, both in :func:`_drain_exclusions`. And every member of
    another Job this owner still holds: the installer supervisor and its worker sit in one of those
    (``WindowsJob.anonymous`` in ``bootstrap``), and so now does every *other*
    live browser launch (:func:`contain_browser_launch`).

    That last exclusion is why this runs after the per-launch Job rather than
    instead of it. A browser's own Job is closed by the time this is reached, so
    its escapees -- anything the Job never held -- are still in scope here,
    while a concurrent launch's contained processes are not. Attribution comes
    from the Job that was assigned before Chromium existed; this is the sweep
    for what got past it.
    """
    if _adopted_windows_job is None:
        return True
    win32api, win32con, win32job, _winerror = _windows_modules()
    spared = _drain_exclusions()
    while True:
        answered = True
        try:
            members = win32job.QueryInformationJobObject(
                _adopted_windows_job, win32job.JobObjectBasicProcessIdList
            )
        except BaseException:  # noqa: BLE001 - an unanswered Job is not an empty one
            answered = False
            members = ()
        remaining = 0
        for process in (int(entry) for entry in members if entry is not None):
            if process in spared:
                continue
            handle: Any | None = None
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_TERMINATE
                    | win32con.PROCESS_QUERY_LIMITED_INFORMATION,
                    False,
                    process,
                )
                if not win32job.IsProcessInJob(handle, _adopted_windows_job):
                    # The id left the Job between the query and here, so it
                    # names somebody else's process now.
                    continue
                if _in_another_owned_job(win32job, handle):
                    continue
                remaining += 1
                win32api.TerminateProcess(handle, 1)
            except BaseException:  # noqa: BLE001 - the next Job query proves exit
                remaining += 1
            finally:
                if handle is not None:
                    with contextlib.suppress(BaseException):
                        handle.Close()
        if answered and remaining == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_JOB_POLL_SECONDS)


def drain_browser_process_marker(
    marker: str,
    *,
    containment: WindowsJob | None = None,
    timeout: float = _MARKER_DRAIN_SECONDS,
) -> bool:
    """Prove one browser launch left nothing of itself running.

    A Patchright close that returns normally is not that proof. Measured in the
    driver it ships (1.61.2, ``packages/utils/processLauncher.ts`` in
    ``lib/coreBundle.js``): Chromium is spawned ``detached`` into its own POSIX
    group, and ``gracefullyClose`` waits for the *leader* it spawned to emit
    ``close`` and for the temporary directories to go. The one call that signals
    the whole group, ``process.kill(-pid, 'SIGKILL')``, runs only when the
    graceful attempt rejects. So the group can outlive a clean close, and
    nothing in the API says whether it did.

    This answers it, for this launch and no other: on POSIX from *marker*, and
    on Windows from *containment*, the Job assigned to this launch's driver
    before it could spawn anything. Blocking: the caller runs it off the event
    loop.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    if _IS_WINDOWS:
        if not _drain_windows_browser_job(containment, deadline):
            return False
        return _drain_adopted_windows_job_members(deadline)
    if marker not in _registered_browser_markers:
        # Nothing was ever registered under it, so no launch got far enough to
        # leave a group behind. Not an optimisation: a scan for a marker that
        # was never handed to a browser is a scan of every process on the
        # machine for an answer that cannot be there.
        return True
    return _drain_marked_posix_groups(marker, deadline)


def hard_exit_process_tree(status: int) -> NoReturn:
    """Drain managed descendants before this process releases ownership locks."""
    if _IS_WINDOWS:
        _drain_adopted_windows_job()
    else:
        pid = os.getpid()
        try:
            process_group = os.getpgrp()
            if process_group == pid:
                # Chromium is deliberately spawned detached by Patchright. Kill and
                # drain those groups while this owner still holds the daemon and
                # profile locks, then end the owner's own group.
                groups = _kill_registered_process_groups()
                _wait_for_process_groups(groups)
                os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
    # The Windows Job now contains only this owner. On POSIX this is the fallback
    # when the caller was not launched as the expected group leader.
    os._exit(status)


def release_nonce() -> str:
    """Return an unpredictable process-gate release token."""
    return secrets.token_hex(_RELEASE_NONCE_BYTES)


def windows_gate_command(target: list[str], nonce: str) -> list[str]:
    """Wrap *target* in the stdlib-only isolated release gate."""
    gate = Path(__file__).with_name("process_gate.py").resolve()
    return [sys.executable, "-I", "-S", "-u", str(gate), nonce, "--", *target]


def release_windows_gate(stream: Any, nonce: str) -> None:
    """Release one assigned gate without consuming target stdin."""
    stream.write(f"release {nonce}\n".encode("ascii"))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


class WindowsJob:
    """A parent-owned kill-on-close Windows Job Object."""

    def __init__(
        self,
        handle: Any,
        *,
        name: str | None,
        win32api: Any,
        win32job: Any,
    ) -> None:
        self._handle = handle
        self.name = name
        self._win32api = win32api
        self._win32job = win32job
        # Whether this Job was seen holding nothing before its handle went. Kept
        # past ``close()`` because a browser close can be retried after its
        # result was lost to a cancel, and a closed Job cannot be queried again:
        # without this the retry would report an unproven shutdown and keep a
        # profile that is provably free.
        self._drained = False

    @classmethod
    def anonymous(cls) -> WindowsJob:
        """Create an unnamed Job for one contained run of something.

        Two callers, and the anonymity is what keeps them apart: an installer
        run (``bootstrap``) and a browser launch
        (:func:`contain_browser_launch`). Neither Job can be opened by name, so
        neither can reach into the other, and each is terminated only through
        the handle its own creator holds. That is the whole of the separation
        the browser drain relies on when it ends its Job without touching a
        download in progress.

        Use :meth:`named` instead where another process has to find the Job:
        only the owner handoff does, and only because the process that creates
        that Job is not the process that lives in it.
        """
        return cls._create(None)

    @classmethod
    def named(cls, purpose: str) -> WindowsJob:
        """Create a collision-checked named Job for owner handoff."""
        win32api, win32con, win32job, winerror = _windows_modules()
        for _ in range(_JOB_NAME_ATTEMPTS):
            name = f"Local\\linkedin-mcp-{purpose}-{secrets.token_hex(16)}"
            try:
                handle = win32job.CreateJobObject(None, name or "")
            except Exception as exc:
                raise ProcessTreeError(
                    "Windows could not create a named Job Object"
                ) from exc
            if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
                handle.Close()
                continue
            return cls._configure(
                handle,
                name=name,
                win32api=win32api,
                win32con=win32con,
                win32job=win32job,
            )
        raise ProcessTreeError("Windows could not create a unique named Job Object")

    @classmethod
    def _create(cls, name: str | None) -> WindowsJob:
        win32api, win32con, win32job, _winerror = _windows_modules()
        try:
            handle = win32job.CreateJobObject(None, name or "")
        except Exception as exc:
            raise ProcessTreeError("Windows could not create a Job Object") from exc
        return cls._configure(
            handle,
            name=name,
            win32api=win32api,
            win32con=win32con,
            win32job=win32job,
        )

    @classmethod
    def _configure(
        cls,
        handle: Any,
        *,
        name: str | None,
        win32api: Any,
        win32con: Any,
        win32job: Any,
    ) -> WindowsJob:
        try:
            win32api.SetHandleInformation(handle, win32con.HANDLE_FLAG_INHERIT, 0)
            limits = win32job.QueryInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation
            )
            limits["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                handle, win32job.JobObjectExtendedLimitInformation, limits
            )
        except Exception as exc:
            handle.Close()
            raise ProcessTreeError("Windows could not configure a Job Object") from exc
        job = cls(
            handle,
            name=name,
            win32api=win32api,
            win32job=win32job,
        )
        # Registered while it is open, so a browser close can tell this owner's
        # deliberate children from a browser's leftovers. See
        # :func:`_drain_adopted_windows_job_members`.
        _live_windows_jobs.append(job)
        return job

    @property
    def closed(self) -> bool:
        return self._handle is None

    @property
    def drained(self) -> bool:
        """Whether this Job was proved empty before its handle was released."""
        return self._drained

    @property
    def job_handle(self) -> Any | None:
        """The open Job handle, or None once this owner has let it go."""
        return self._handle

    def assign_popen(self, child: subprocess.Popen[Any]) -> None:
        """Assign and verify a real ``Popen`` process handle."""
        process_handle = getattr(child, "_handle", None)
        if process_handle is None:
            raise ProcessTreeError("Windows Popen exposed no process handle")
        self._assign_handle(process_handle)

    def assign_asyncio_process(self, child: Any) -> subprocess.Popen[Any]:
        """Assign an asyncio child through its transport's real ``Popen``."""
        transport = getattr(child, "_transport", None)
        get_extra_info = getattr(transport, "get_extra_info", None)
        if not callable(get_extra_info):
            raise ProcessTreeError("Windows asyncio exposed no subprocess transport")
        popen = get_extra_info("subprocess")
        if popen is None or getattr(popen, "_handle", None) is None:
            raise ProcessTreeError("Windows asyncio exposed no underlying Popen")
        self.assign_popen(popen)
        return popen

    def _assign_handle(self, process_handle: Any) -> None:
        handle = self._require_handle()
        try:
            self._win32job.AssignProcessToJobObject(handle, process_handle)
            assigned = self._win32job.IsProcessInJob(process_handle, handle)
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not assign the managed process"
            ) from exc
        if not assigned:
            raise ProcessTreeError(
                "Windows did not retain the managed process in its Job"
            )

    def terminate(self) -> None:
        """Request unconditional termination of every process in the Job."""
        handle = self._require_handle()
        last_error: BaseException | None = None
        for attempt in range(_JOB_API_FAILURE_LIMIT):
            try:
                self._win32job.TerminateJobObject(handle, 1)
                return
            except BaseException as exc:  # noqa: BLE001 - wrapped after retries
                last_error = exc
            if attempt + 1 < _JOB_API_FAILURE_LIMIT:
                time.sleep(_JOB_POLL_SECONDS)
        raise ProcessTreeError("Windows could not terminate the managed Job") from (
            last_error
        )

    def release_popen_handle(self, child: subprocess.Popen[Any]) -> None:
        """Release an exited CPython ``Popen`` process-object reference."""
        if child.returncode is None:
            raise ProcessTreeError(
                "Windows cannot release a process handle before process exit"
            )
        process_handle = getattr(child, "_handle", None)
        close = getattr(process_handle, "Close", None)
        if not callable(close):
            raise ProcessTreeError("Windows Popen exposed no releasable process handle")
        try:
            close()
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not release the managed process handle"
            ) from exc

    def wait_until_empty(self, *, timeout: float) -> None:
        """Close the Job after bounded proof that no process remains associated."""
        handle = self._require_handle()
        deadline = time.monotonic() + max(timeout, 0.0)
        consecutive_failures = 0
        while True:
            try:
                accounting = self._win32job.QueryInformationJobObject(
                    handle, self._win32job.JobObjectBasicAccountingInformation
                )
                active = int(accounting["ActiveProcesses"])
            except BaseException as exc:  # noqa: BLE001 - never treated as empty
                consecutive_failures += 1
                if consecutive_failures >= _JOB_API_FAILURE_LIMIT:
                    self._retain()
                    raise ProcessTreeError(
                        "Windows could not verify that the managed Job drained"
                    ) from exc
            else:
                consecutive_failures = 0
                if active == 0:
                    self._drained = True
                    self.close()
                    return

            if time.monotonic() >= deadline:
                self._retain()
                raise ProcessTreeError(
                    "The managed Windows Job did not drain before its deadline"
                )
            time.sleep(min(_JOB_POLL_SECONDS, max(deadline - time.monotonic(), 0.0)))

    def _retain(self) -> None:
        """Keep containment authority alive after drainage cannot be proved."""
        if self not in _retained_windows_jobs:
            _retained_windows_jobs.append(self)

    def close(self) -> None:
        """Close this owner handle without claiming the Job drained."""
        handle, self._handle = self._handle, None
        if handle is not None:
            handle.Close()
        with contextlib.suppress(ValueError):
            _retained_windows_jobs.remove(self)
        with contextlib.suppress(ValueError):
            _live_windows_jobs.remove(self)

    def _require_handle(self) -> Any:
        if self._handle is None:
            raise ProcessTreeError("The Windows Job handle is already closed")
        return self._handle

    @staticmethod
    def verify_current_process(name: str) -> None:
        """Verify exact named-Job membership without retaining a handle."""
        win32api, _win32con, win32job, _winerror = _windows_modules()
        try:
            handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name)
            try:
                assigned = win32job.IsProcessInJob(win32api.GetCurrentProcess(), handle)
            finally:
                handle.Close()
        except Exception as exc:
            raise ProcessTreeError(
                "Windows could not verify owner Job membership"
            ) from exc
        if not assigned:
            raise ProcessTreeError("The owner is not a member of its named Windows Job")

    @staticmethod
    def adopt_current_process(name: str) -> None:
        """Retain a verified named Job handle until process teardown."""
        global _adopted_windows_job, _adopted_windows_gate
        if _adopted_windows_job is not None:
            raise ProcessTreeError("The owner already adopted a Windows Job")
        win32api, _win32con, win32job, _winerror = _windows_modules()
        handle: Any | None = None
        try:
            handle = win32job.OpenJobObject(win32job.JOB_OBJECT_QUERY, False, name)
            if not win32job.IsProcessInJob(win32api.GetCurrentProcess(), handle):
                raise ProcessTreeError(
                    "The owner is not a member of its named Windows Job"
                )
            _adopted_windows_job = int(handle.Detach())
            _adopted_windows_gate = os.getppid()
            handle = None
        except ProcessTreeError:
            raise
        except Exception as exc:
            raise ProcessTreeError("Windows could not adopt the owner Job") from exc
        finally:
            if handle is not None:
                handle.Close()


def _windows_modules() -> tuple[Any, Any, Any, Any]:
    if os.name != "nt":
        raise ProcessTreeError("Windows Job Objects do not exist on this platform")
    try:
        return (
            importlib.import_module("win32api"),
            importlib.import_module("win32con"),
            importlib.import_module("win32job"),
            importlib.import_module("winerror"),
        )
    except Exception as exc:
        raise ProcessTreeError("pywin32 Job Object APIs are unavailable") from exc


class _ProcBsdInfo(ctypes.Structure):
    """Darwin's ``struct proc_bsdinfo`` through the start timestamp fields."""

    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _kernel_start_identity(pid: int) -> str | None:
    """Return a process start identity precise enough to reject PID reuse."""
    if sys.platform == "darwin":
        try:
            libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
            proc_pidinfo = libproc.proc_pidinfo
            proc_pidinfo.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint64,
                ctypes.c_void_p,
                ctypes.c_int,
            ]
            proc_pidinfo.restype = ctypes.c_int
            info = _ProcBsdInfo()
            read = proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        except (AttributeError, OSError):
            return None
        if read != ctypes.sizeof(info):
            return None
        return f"darwin:{info.pbi_start_tvsec}:{info.pbi_start_tvusec}"

    try:
        raw = (Path(f"/proc/{pid}/stat")).read_text()
    except (OSError, UnicodeError):
        return None
    closing = raw.rfind(")")
    fields = raw[closing + 2 :].split() if closing >= 0 else []
    return f"proc:{fields[19]}" if len(fields) > 19 else None


def process_group_exists(pgid: int) -> bool:
    """Whether POSIX still has any process in *pgid*."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def process_group_has_live_member(pgid: int) -> bool:
    """Whether *pgid* still holds a process the kernel can schedule.

    :func:`process_group_exists` cannot answer this. A process group exists for
    as long as any member is in the process table, and an unreaped zombie is
    still in it: measured in a plain Linux container, a target killed through
    its group leaves one ``state=Z`` entry reparented to a PID 1 that never
    reaps, and ``killpg(pgid, 0)`` then reports that group alive forever. A
    settlement loop polling only the group's existence spends its whole budget
    on a group in which no code can run, and the installer supervisor turns
    that into exit status 70 for an install that actually succeeded.

    This is the distinction :func:`_registered_group_still_matches` already
    draws for browser drains, applied to plain group settlement. Only the
    zombie is discounted: stopped, traced and uninterruptible members are
    processes that can still be resumed and still hold what they opened, so
    they keep blocking. So does anything the snapshot cannot answer, because
    not knowing is not evidence that a group is empty.

    Identity is deliberately not consulted here. The question is whether *any*
    live process sits in this group number, which is true or false regardless
    of who that process is; the PID and PGID identity checks in
    :func:`_reap_and_wait_for_group` are what decide whose group it is, and
    they are left alone.
    """
    try:
        rows = _posix_process_rows()
    except OSError:
        return True
    members = [(process, row) for process, row in rows.items() if row[1] == pgid]
    if not members:
        # An empty snapshot means the reader failed, and a group that ``killpg``
        # still reports while no row names it is a snapshot taken mid-exit.
        # Neither is proof of an empty group; let the caller keep polling.
        return True
    return any(not _has_exited_unreaped(process, row[3]) for process, row in members)


def _kqueue_name(name: str) -> Any:
    """Look up a kqueue name so a non-BSD type check cannot resolve it.

    kqueue is a BSD facility and none of these names exist in the Linux
    ``select``, which is the platform the type checker runs on in CI. A direct
    reference is an error there for a function no Linux process can reach, and
    pinning the checker to one platform only moves the error to the Windows
    code next door.
    """
    return getattr(select, name)


def _darwin_child_exited_without_reaping(pid: int) -> bool:
    """Observe NOTE_EXIT without collecting the Darwin child."""
    kqueue = _kqueue_name("kqueue")()
    try:
        event = _kqueue_name("kevent")(
            pid,
            filter=_kqueue_name("KQ_FILTER_PROC"),
            flags=_kqueue_name("KQ_EV_ADD") | _kqueue_name("KQ_EV_ENABLE"),
            fflags=_kqueue_name("KQ_NOTE_EXIT"),
        )
        try:
            kqueue.control([event], 0, 0)
        except ProcessLookupError:
            # A child that has exited but has not been waited for no longer accepts
            # a new process filter. Its zombie still pins the pid for its parent.
            return True
        return bool(kqueue.control(None, 1, 0))
    finally:
        kqueue.close()


def child_exited_without_reaping(child: subprocess.Popen[Any]) -> bool:
    """Observe a POSIX child exit while its zombie still pins the numeric pid."""
    if os.name == "nt":
        return child.poll() is not None
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        if sys.platform == "darwin":
            return _darwin_child_exited_without_reaping(child.pid)
        raise ProcessTreeError("POSIX child identity cannot be observed safely")
    try:
        result = waitid(
            os.P_PID,
            child.pid,
            os.WEXITED | os.WNOHANG | os.WNOWAIT,
        )
    except ChildProcessError:
        return child.returncode is not None
    return result is not None


def _reap_and_wait_for_group(
    pgid: int,
    child: subprocess.Popen[Any],
    deadline: float,
    leader_identity: str | None,
) -> bool:
    child.wait(timeout=max(deadline - time.monotonic(), 0.0))
    last_run_state_poll: float | None = None
    while process_group_exists(pgid):
        now = time.monotonic()
        if (
            last_run_state_poll is None
            or now - last_run_state_poll >= _GROUP_RUN_STATE_SECONDS
        ):
            last_run_state_poll = now
            # A group holding nothing but unreaped entries has settled: nothing
            # in it can run, open a file or take a lock again, and this owner
            # cannot reap what a foreign parent holds. Without this the loop
            # waits out its whole budget, because the group number outlives
            # every process that ever ran in it.
            if not process_group_has_live_member(pgid):
                return True
        current_identity = _kernel_start_identity(pgid)
        if leader_identity is not None and current_identity not in (
            None,
            leader_identity,
        ):
            try:
                if os.getpgid(pgid) == pgid:
                    return True
            except ProcessLookupError:
                pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def terminate_process_group(
    pgid: int,
    *,
    timeout: float,
    child: subprocess.Popen[Any],
) -> bool:
    """Kill a POSIX child group while its unreaped leader pins the PGID."""
    if os.name == "nt":
        raise ProcessTreeError("POSIX process groups do not exist on Windows")

    leader_identity = _kernel_start_identity(child.pid)
    if child.returncode is not None:
        return False
    waitid = getattr(os, "waitid", None)
    if waitid is not None:
        try:
            waitid(
                os.P_PID,
                child.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            return False
    elif sys.platform == "darwin":
        try:
            child_exited_without_reaping(child)
        except OSError as exc:
            raise ProcessTreeError(
                "Darwin child identity cannot be observed safely"
            ) from exc
    else:
        raise ProcessTreeError("POSIX child identity cannot be observed safely")

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            child.poll()
            return True
        except PermissionError as exc:
            if child_exited_without_reaping(child):
                return _reap_and_wait_for_group(pgid, child, deadline, leader_identity)
            logger.warning("The process group %s could not be signalled: %s", pgid, exc)
            return False

        if child_exited_without_reaping(child):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return _reap_and_wait_for_group(pgid, child, deadline, leader_identity)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
