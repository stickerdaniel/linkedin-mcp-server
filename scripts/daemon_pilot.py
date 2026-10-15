"""Record one pilot session of the shared-browser daemon (release gate 3).

Run with the candidate's own interpreter, as ``docs/daemon-pilot.md`` shows:

    python daemon_pilot.py before --pilot-dir D --host NAME --others-stopped \\
        --candidate "uvx --isolated --cache-dir C --from git+...@SHA mcp-server-linkedin"
    # start the host, read LinkedIn through it, quit the host
    python daemon_pilot.py after --pilot-dir D --session ID [--attest login@TIME]
    python daemon_pilot.py tally --sha SHA D [D2 ...]

How a session is read is decided in
``docs/decisions/2026-10-15-daemon-gates-2-and-3-procedure.md``.

Read-only towards the session: nothing is written under the auth root or the
daemon state, no cookie value is stored, and no helper that prepares, claims,
locks or prunes is imported. The only writes are new files in a session
folder of the pilot directory, never through a link. ``--status`` is the one
active step, run through the candidate, and its whole process tree ends with
it.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.metadata
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from collections.abc import Collection, Iterable, Sequence
from pathlib import Path
from typing import IO, Any, Literal, NamedTuple

from linkedin_mcp_server.daemon.descriptor import daemon_state_root, profile_identity
from linkedin_mcp_server.process_tree import (
    ProcessTreeError,
    WindowsJob,
    release_nonce,
    release_windows_gate,
    terminate_process_group,
    windows_gate_command,
)
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    auth_root_dir,
    canonical,
    get_runtime_id,
    portable_cookie_path,
    source_state_path,
)

Mode = Literal["daemon", "fallback", "uncertain"]
StatusVerdict = Literal["valid", "not-verified", "busy", "auth-failed", "error"]

#: The host configuration every pilot session declares. The owner retires after
#: the idle timeout, which ``after`` waits out.
HOST_CONFIG: dict[str, Any] = {
    "args": ["--daemon", "--log-level", "INFO", "--browser-idle-timeout", "600"],
    "env": {"LINKEDIN_TRACE_MODE": "always"},
}
QUOTAS = {"macos": 25, "linux": 20, "windows": 15}

#: Twice the declared idle timeout. Never followed by a kill.
_IDLE_WAIT_SECONDS = 20 * 60
_POLL_SECONDS = 5.0
#: ``--status`` may install the browser first on a fresh resolution.
_STATUS_SECONDS = 15 * 60
#: How long ending the status command's process tree may take.
_TREE_SECONDS = 10.0
_STATUS_OUTPUT_CHARS = 20_000
_DESCRIPTOR_BYTES = 64 * 1024
_SESSION_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
#: The folder in a session folder that holds what the host session left.
_CAPTURE = "capture"
_ATTESTED = ("login", "logout", "import")
_CANDIDATE_SHA = re.compile(r"git\+\S+@([0-9a-fA-F]{40})(?![0-9a-zA-Z])")

# The CLI's own lines: a profile command logs the banner with its mode.
_COMMAND = re.compile(r"LinkedIn MCP Server v\S+ - (.+) mode")
_STATUS_VERSION = re.compile(r"LinkedIn MCP Server v(\S+) - Session Info mode")
#: Commands that change the session, so a session running one does not qualify.
_SESSION_CHANGING = frozenset({"Session Creation", "Profile Clear", "Browser Import"})

# The startup decision of a frontend (cli_main, daemon.discovery and
# daemon.election). Only the first one counts: a proxy that later fails to
# recover logs other lines and stays daemon.
_FORWARDING = "Forwarding to the shared browser owner"
_FALLBACK_EXACT = frozenset(
    {
        "The shared browser owner is unavailable",
        "Could not decide whether to share a browser; this server drives its own browser",
    }
)
_FALLBACK_PREFIXES = (
    "No shared browser owner could be started (",
    "Leaving the shared browser owner alone: ",
    "DAEMON_ENABLED is ignored in a container",
)
_FALLBACK_SUFFIX = "this server drives its own browser instead of sharing one"

#: Tool entry lines the owner logs for a call that changes something on LinkedIn.
#: Every other INFO line under ``linkedin_mcp_server.tools`` is a read.
_WRITING_TOOL_LINES = ("Connecting with person: ", "Sending message to ")

_PREFLIGHT_NOTE = (
    "A missing or dead descriptor does not prove that no owner is starting; "
    "the tester confirmed that every other host is stopped."
)


class RecorderError(Exception):
    """A refusal, printed without a traceback."""


class _Refused(Exception):
    """A log the recorder will not read: a link, not a file, or out of place."""


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _os_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


def _inside(path: Path, root: Path) -> bool:
    """Whether *path* is *root* or below it, symlinks resolved."""
    child = Path(os.path.normcase(canonical(path)))
    parent = Path(os.path.normcase(canonical(root)))
    return child == parent or parent in child.parents


def _refuse_protected(path: Path, auth_root: Path) -> None:
    """Refuse a destination under the auth root or the account's daemon state.

    Checked for every directory the recorder writes into, with links
    resolved, so neither a pilot directory nor a link inside it can aim a
    write at the session stores, a descriptor or a token.
    """
    for root in (auth_root, daemon_state_root().parent):
        if _inside(path, root):
            raise RecorderError(
                f"{path} is inside {root}, which the recorder never writes to. "
                "Choose a pilot directory outside it."
            )


def _create(path: Path, data: bytes, auth_root: Path) -> None:
    """Write *data* to a new file. Never follows or replaces an existing entry."""
    _refuse_protected(path.parent, auth_root)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    with open(os.open(path, flags, 0o600), "wb") as handle:
        handle.write(data)


def _open_log(path: Path, root: Path) -> IO[bytes]:
    """Open a log for reading, if it is a regular file that resolves under *root*.

    Raises ``FileNotFoundError`` when there is none and ``_Refused`` for a
    link, anything that is not a regular file, or a path whose directories
    lead out of *root*: copying any of those could put a token or the cookie
    store into the evidence.
    """
    if not stat.S_ISREG(path.lstat().st_mode):
        raise _Refused("not a regular file")
    if not _inside(path, root):
        raise _Refused(f"resolves outside {root}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    handle = open(os.open(path, flags), "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise _Refused("not a regular file")
    return handle


# --- Passive snapshot -------------------------------------------------------


def _read_json(path: Path) -> tuple[str, Any]:
    """``("absent", None)``, ``("unreadable", reason)`` or ``("present", data)``."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return "absent", None
    except OSError as exc:
        return "unreadable", type(exc).__name__
    try:
        return "present", json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return "unreadable", type(exc).__name__


def _source_state(path: Path) -> dict[str, Any]:
    state, data = _read_json(path)
    if state != "present":
        return {"state": state, "reason": data} if data else {"state": state}
    if not isinstance(data, dict):
        return {"state": "unreadable", "reason": "not an object"}
    fields = {
        name: data.get(name) for name in ("login_generation", "source_runtime_id")
    }
    if not all(isinstance(value, str) for value in fields.values()):
        return {"state": "unreadable", "reason": "missing fields"}
    return {"state": "present", **fields}


def _cookies(path: Path) -> dict[str, Any]:
    """The file's hash and whether ``li_at`` is in it. Never a value."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"state": "absent"}
    except OSError as exc:
        return {"state": "unreadable", "reason": type(exc).__name__}
    digest = hashlib.sha256(raw).hexdigest()
    try:
        cookies = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"state": "unreadable", "reason": type(exc).__name__, "sha256": digest}
    if not isinstance(cookies, list):
        return {"state": "unreadable", "reason": "not a list", "sha256": digest}
    names = {cookie.get("name") for cookie in cookies if isinstance(cookie, dict)}
    return {"state": "present", "sha256": digest, "li_at": "li_at" in names}


def _profile(profile: Path) -> dict[str, Any]:
    try:
        if not profile.is_dir():
            return {"state": "absent"}
        with os.scandir(profile) as entries:
            empty = next(entries, None) is None
    except OSError as exc:
        return {"state": "unreadable", "reason": type(exc).__name__}
    return {"state": "empty" if empty else "present"}


def _last_version(profile: Path) -> dict[str, Any]:
    try:
        text = (profile / "Last Version").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return {"state": "absent"}
    except (OSError, UnicodeDecodeError) as exc:
        return {"state": "unreadable", "reason": type(exc).__name__}
    return {"state": "present", "value": text[:64]}


def _quarantines(auth_root: Path) -> dict[str, Any]:
    try:
        with os.scandir(auth_root) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if entry.name.startswith(QUARANTINE_PREFIX)
            )
    except FileNotFoundError:
        return {"state": "absent", "names": []}
    except OSError as exc:
        return {"state": "unreadable", "reason": type(exc).__name__}
    return {"state": "present", "names": names}


def passive_snapshot(profile: Path) -> dict[str, Any]:
    """Read the session stores without opening a browser or writing anything."""
    auth_root = auth_root_dir(profile)
    return {
        "taken_at": _now(),
        "source_state": _source_state(source_state_path(profile)),
        "cookies": _cookies(portable_cookie_path(profile)),
        "profile": _profile(profile),
        "last_version": _last_version(profile),
        "quarantines": _quarantines(auth_root),
    }


# --- --status through the candidate -----------------------------------------


def classify_status(exit_code: int | None, output: str) -> StatusVerdict:
    """Read the candidate's ``--status`` the way the gate does.

    Only exit 0 together with the CLI's own valid line is valid: a foreign
    runtime also exits 0, after saying that it did not check the cookies.
    """
    if exit_code == 0:
        return "valid" if "Session is valid (profile:" in output else "not-verified"
    if exit_code == 1:
        if "Another process holds the browser profile" in output:
            return "busy"
        if (
            "No valid source session found" in output
            or "Session expired or invalid" in output
        ):
            return "auth-failed"
    return "error"


def _split(candidate: str) -> list[str]:
    # shlex would eat the backslashes of a Windows path, so there the
    # command line is split the way Windows itself splits one.
    if os.name == "nt":
        return _windows_split(candidate)
    return shlex.split(candidate)


def _windows_split(command: str) -> list[str]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    shell32 = getattr(ctypes, "WinDLL")("shell32", use_last_error=True)
    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    shell32.CommandLineToArgvW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_int),
    ]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    argv = shell32.CommandLineToArgvW(command, ctypes.byref(count))
    if not argv:
        raise RecorderError(f"The candidate {command!r} is not a command line")
    try:
        return [argv[index] for index in range(count.value)]
    finally:
        kernel32.LocalFree(argv)


def _end_tree(child: subprocess.Popen[bytes], job: WindowsJob | None) -> bool:
    """Kill everything *child* started, and say whether that was proved.

    A POSIX group is signalled only while its leader is unreaped, because
    only that keeps its number from naming someone else's group. An
    interruption can land after ``communicate`` reaped the leader; then
    nothing is signalled and the tree counts as not ended.
    """
    try:
        if job is not None:
            job.terminate()
            job.wait_until_empty(timeout=_TREE_SECONDS)
            return True
        if child.returncode is None:
            return terminate_process_group(
                child.pid, timeout=_TREE_SECONDS, child=child
            )
    except ProcessTreeError:
        pass
    return False


def _warn_unended() -> None:
    print(
        "The status command was stopped and part of it may still be running; "
        "check for a leftover mcp-server-linkedin process.",
        file=sys.stderr,
    )


def _supervised(command: list[str]) -> tuple[int | None, bytes, dict[str, Any]]:
    """Run *command* to its end, or end its whole process tree.

    ``uvx`` starts the candidate as a child of its own, so killing ``uvx``
    alone would leave the candidate running. The command gets a POSIX session
    or a Windows Job of its own, and on the timeout or an interruption the
    whole of it goes. The output is decoded later as UTF-8.
    """
    # A cp1252 console would turn the CLI's check mark into an exception and
    # a valid session into an error.
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    job = WindowsJob.anonymous() if os.name == "nt" else None
    try:
        if job is not None:  # pragma: no cover - Windows only
            # The gate holds the command until it is in the Job, so nothing it
            # starts can escape. Its input then ends, as DEVNULL would.
            nonce = release_nonce()
            child = subprocess.Popen(
                windows_gate_command(command, nonce),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            assert child.stdin is not None
            try:
                job.assign_popen(child)
                release_windows_gate(child.stdin, nonce)
                child.stdin.close()
            except BaseException:
                child.kill()
                _end_tree(child, job)
                raise
        else:
            child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
    except BaseException:
        if job is not None:
            job.close()
        raise
    try:
        out, err = child.communicate(timeout=_STATUS_SECONDS)
    except BaseException as exc:
        ended = _end_tree(child, job)
        if not ended:
            _warn_unended()
        try:
            out, err = child.communicate(timeout=_TREE_SECONDS)
        except subprocess.TimeoutExpired as late:
            # Something outside the tree still holds the pipes.
            out, err = late.stdout or b"", late.stderr or b""
            child.kill()
            child.wait()
        if not isinstance(exc, subprocess.TimeoutExpired):
            raise
        return None, out + err, {"timed_out": True, "tree_ended": ended}
    finally:
        if job is not None:
            job.close()
    return child.returncode, out + err, {"timed_out": False}


def run_status(candidate: str, profile: Path) -> dict[str, Any]:
    """Run ``--status`` through the candidate and keep its bounded output."""
    extra = ["--status", "--log-level", "INFO", "--user-data-dir", str(profile)]
    command = [*_split(candidate), *extra]
    started = _now()
    exit_code: int | None
    ending: dict[str, Any] = {"timed_out": False}
    try:
        exit_code, raw, ending = _supervised(command)
    except (OSError, ProcessTreeError) as exc:
        exit_code, output = None, f"[could not start: {exc}]"
    else:
        output = raw.decode("utf-8", errors="replace")
        if ending["timed_out"]:
            output += "\n[timed out]"
    version = _STATUS_VERSION.search(output)
    return {
        "started_at": started,
        "command": command,
        "exit": exit_code,
        "verdict": classify_status(exit_code, output),
        "package_version": version.group(1) if version else None,
        "output": output[-_STATUS_OUTPUT_CHARS:],
        "output_truncated": len(output) > _STATUS_OUTPUT_CHARS,
        **ending,
    }


# --- Owners and their log ---------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Whether a process with *pid* exists. Not knowing counts as yes."""
    if pid <= 0:
        return True
    if os.name == "nt":
        # Never os.kill there: any signal but the console ones terminates.
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _windows_pid_alive(pid: int) -> bool:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    process_query_limited_information, still_active, access_denied = 0x1000, 259, 5
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return getattr(ctypes, "get_last_error")() == access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _descriptors() -> list[dict[str, Any]]:
    """Every published descriptor of this account, read without following links.

    Owners never remove theirs, so a dead owner's descriptor stays and names
    the directory its log is in.
    """
    root = daemon_state_root()
    found: list[dict[str, Any]] = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except FileNotFoundError:
        return found
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(entry.path) / "daemon.json"
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_DESCRIPTOR_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError as exc:
            found.append({"path": str(path), "unreadable": type(exc).__name__})
            continue
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if (
            len(raw) > _DESCRIPTOR_BYTES
            or not isinstance(data, dict)
            or not isinstance(data.get("profile_path"), str)
            or not isinstance(data.get("profile_identity"), str)
            or type(data.get("pid", 0)) is not int
        ):
            found.append({"path": str(path), "unreadable": "not a descriptor"})
            continue
        found.append(
            {
                "path": str(path),
                "pid": data.get("pid", 0),
                "profile_path": data["profile_path"],
                "profile_identity": data["profile_identity"],
                "package_version": data.get("package_version"),
            }
        )
    return found


def _in_scope(descriptor: dict[str, Any], auth_root: Path) -> bool:
    """Whether *descriptor* belongs to *auth_root*, which elects one owner."""
    try:
        return canonical(Path(descriptor["profile_path"])).parent == canonical(
            auth_root
        )
    except (OSError, RuntimeError, ValueError):
        return True


def live_owners(auth_root: Path) -> list[dict[str, Any]]:
    """Descriptors of this auth root whose process may still be running.

    Conservative: any live PID counts, without comparing ``started_at``, which
    is when the descriptor was assembled rather than when its process began.
    An unreadable descriptor cannot be placed, so it counts as live too.
    """
    return [
        {
            key: descriptor[key]
            for key in ("path", "pid", "unreadable")
            if key in descriptor
        }
        for descriptor in _descriptors()
        if "unreadable" in descriptor
        or (_in_scope(descriptor, auth_root) and _pid_alive(descriptor["pid"]))
    ]


def _owner_log(profile: Path) -> Path | None:
    """The daemon.log beside the descriptor that serves *profile*."""
    try:
        identity = profile_identity(profile)
    except Exception:
        return None
    matches = [
        Path(descriptor["path"])
        for descriptor in _descriptors()
        if descriptor.get("profile_identity") == identity
    ]
    if not matches:
        return None
    newest = max(matches, key=lambda path: path.stat().st_mtime if path.exists() else 0)
    return newest.parent / "daemon.log"


def _prefix_digest(handle: IO[bytes], length: int) -> str:
    """SHA-256 of the first *length* bytes behind *handle*."""
    handle.seek(0)
    digest = hashlib.sha256()
    remaining = length
    while remaining > 0:
        chunk = handle.read(min(remaining, 1 << 20))
        if not chunk:
            break
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def owner_log_position(profile: Path) -> dict[str, Any]:
    """Where the owner log of *profile* ends now, which file that is, and what
    it holds up to there, so a log cut short and grown again is told apart
    from one that only grew."""
    path = _owner_log(profile)
    if path is None:
        return {"path": None}
    try:
        with _open_log(path, daemon_state_root()) as handle:
            info = os.fstat(handle.fileno())
            prefix = _prefix_digest(handle, info.st_size)
    except FileNotFoundError:
        return {"path": str(path), "identity": None, "offset": 0}
    except _Refused as exc:
        return {"path": str(path), "refused": str(exc)}
    return {
        "path": str(path),
        "identity": [info.st_dev, info.st_ino],
        "offset": info.st_size,
        "prefix_sha256": prefix,
    }


def read_log_records(text: str) -> tuple[list[dict[str, Any]], int]:
    """JSON log records in *text*, and how many lines were something else.

    The owner's stdout and stderr go to its log as well, so not every line is
    one of the JSON records the logging configuration writes.
    """
    records: list[dict[str, Any]] = []
    other = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            other += 1
            continue
        if isinstance(record, dict) and isinstance(record.get("message"), str):
            records.append(record)
        else:
            other += 1
    return records, other


def tool_calls(records: Iterable[dict[str, Any]]) -> dict[str, int]:
    """How many tool calls the owner logged on entry, reads and writes apart."""
    counts = {"reading": 0, "writing": 0}
    for record in records:
        logger = record.get("logger")
        if record.get("level") != "INFO" or not isinstance(logger, str):
            continue
        if not logger.startswith("linkedin_mcp_server.tools."):
            continue
        writing = record["message"].startswith(_WRITING_TOOL_LINES)
        counts["writing" if writing else "reading"] += 1
    return counts


def copy_owner_log(
    before: dict[str, Any], profile: Path, folder: Path, auth_root: Path
) -> tuple[dict[str, Any], set[str]]:
    """Copy the part of the owner log written since ``before``, and judge the file.

    A different file at that path, a shorter one, one whose bytes up to the
    old end changed, a log in another directory, or one that is not a regular
    file each mean the window cannot be trusted. Also returns the window's
    lines, which attribute the owner's own trace run.
    """
    path = _owner_log(profile)
    result: dict[str, Any] = {"path": str(path) if path else None, "state": "absent"}
    result["tool_calls"] = {"reading": 0, "writing": 0}
    try:
        if path is None:
            raise FileNotFoundError
        handle = _open_log(path, daemon_state_root())
    except FileNotFoundError:
        if before.get("identity") is not None or "refused" in before:
            result["state"] = "missing"
        return result, set()
    except _Refused as exc:
        result.update(state="refused", reason=str(exc))
        return result, set()
    with handle:
        info = os.fstat(handle.fileno())
        offset = 0
        if "refused" in before:
            result["state"] = "refused"
        elif before["path"] is None or before.get("identity") is None:
            result["state"] = "created"
        elif before["path"] != str(path):
            result["state"] = "moved"
        elif before["identity"] != [info.st_dev, info.st_ino]:
            result["state"] = "replaced"
        elif info.st_size < before["offset"]:
            result["state"] = "truncated"
        elif _prefix_digest(handle, before["offset"]) != before.get("prefix_sha256"):
            result["state"] = "rewritten"
        else:
            result["state"] = "continued"
            offset = before["offset"]
        handle.seek(offset)
        window = handle.read()
    _create(folder / "daemon.log", window, auth_root)
    text = window.decode("utf-8", errors="replace")
    records, other = read_log_records(text)
    result.update(
        {
            "from_offset": offset,
            "bytes": len(window),
            "evidence": "daemon.log",
            "records": len(records),
            "non_json_lines": other,
            "tool_calls": tool_calls(records),
        }
    )
    return result, {line for line in text.splitlines() if line.strip()}


# --- Frontend lifetimes ----------------------------------------------------


def trace_runs(auth_root: Path) -> list[str]:
    try:
        with os.scandir(auth_root / "trace-runs") as entries:
            return sorted(
                entry.name
                for entry in entries
                if entry.name.startswith("run-") and entry.is_dir(follow_symlinks=False)
            )
    except OSError:
        return []


def _decision(message: str) -> Mode | None:
    if message == _FORWARDING:
        return "daemon"
    if (
        message in _FALLBACK_EXACT
        or message.startswith(_FALLBACK_PREFIXES)
        or message.endswith(_FALLBACK_SUFFIX)
    ):
        return "fallback"
    return None


def classify_run(
    text: str, owner_lines: Collection[str] = frozenset()
) -> dict[str, Any]:
    """Say what process wrote one trace run, and a frontend's startup decision.

    A profile command names itself. A run is the owner's only when every line
    of it is also in this session's owner log, since the owner writes the same
    records to both. Any other run with something in it is a frontend, and
    uncertain unless it logged its decision: one below INFO may log nothing,
    or only a warning or an error. An empty run is ``unknown``. Uncertain
    and unknown runs both make the session uncertain.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {"kind": "unknown"}
    records, _ = read_log_records(text)
    mode: Mode | None = None
    for record in records:
        message = record["message"]
        if command := _COMMAND.fullmatch(message):
            return {"kind": "profile_command", "command": command.group(1)}
        if mode is None:
            mode = _decision(message)
    if mode is None and set(lines) <= set(owner_lines):
        return {"kind": "owner"}
    return {"kind": "frontend", "mode": mode or "uncertain"}


def session_mode(runs: Iterable[dict[str, Any]]) -> Mode:
    """A host session's mode from its frontends' startup decisions.

    Uncertain if any frontend is, if any run cannot be placed, or if there is
    no frontend; otherwise fallback if any frontend fell back; daemon only
    when every one forwarded.
    """
    runs = list(runs)
    if any(run["kind"] == "unknown" for run in runs):
        return "uncertain"
    modes = [run["mode"] for run in runs if run["kind"] == "frontend"]
    if not modes or "uncertain" in modes:
        return "uncertain"
    if "fallback" in modes:
        return "fallback"
    return "daemon"


def copy_trace_runs(
    auth_root: Path,
    baseline: Iterable[str],
    folder: Path,
    owner_lines: Collection[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Copy each trace run created since the baseline into *folder*, and classify it.

    *folder* gets a new ``trace-runs`` directory; a run whose log cannot be
    read safely is ``unknown`` and copied as nothing.
    """
    known = set(baseline)
    runs: list[dict[str, Any]] = []
    copies = folder / "trace-runs"
    made = False
    for name in trace_runs(auth_root):
        if name in known:
            continue
        try:
            with _open_log(
                auth_root / "trace-runs" / name / "server.log", auth_root
            ) as log:
                data = log.read()
        except _Refused as exc:
            runs.append({"name": name, "kind": "unknown", "refused": str(exc)})
            continue
        except OSError as exc:
            runs.append(
                {"name": name, "kind": "unknown", "missing": type(exc).__name__}
            )
            continue
        if not made:
            # Created here, so never an entry someone else put in its place.
            _refuse_protected(folder, auth_root)
            copies.mkdir()
            made = True
        (copies / name).mkdir()
        _create(copies / name / "server.log", data, auth_root)
        text = data.decode("utf-8", errors="replace")
        records, _ = read_log_records(text)
        runs.append(
            {
                "name": name,
                **classify_run(text, owner_lines),
                "tool_calls": tool_calls(records),
                "evidence": f"trace-runs/{name}/server.log",
            }
        )
    return runs


# --- Judgement ---------------------------------------------------------------


def judge(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Whether the session qualifies, and its outcome.

    Only the stores and the after-status decide the outcome. Nothing the
    owner logged and nothing the tester attests can turn an incident into a
    retained session; incidents are closed in the gate review, not here.
    """
    counted = bool(before["counts"])
    not_qualifying = list(before["not_counted_because"])
    not_qualifying += [
        f"the tester attested {entry}" for entry in after["attestations"]
    ]
    for run in after["trace_runs"]:
        if run.get("command") in _SESSION_CHANGING:
            not_qualifying.append(f"a {run['command']} command ran ({run['name']})")
    # A daemon session reads through the owner; a frontend that fell back
    # reads in its own process, so its own trace run holds the line.
    reads = after["owner_log"]["tool_calls"]["reading"]
    if after["mode"] != "daemon":
        reads += sum(
            run.get("tool_calls", {}).get("reading", 0)
            for run in after["trace_runs"]
            if run["kind"] == "frontend"
        )
    if reads < 1:
        not_qualifying.append("no LinkedIn-reading tool call was logged")

    reasons: list[str] = []
    if counted:
        reasons = _incident_reasons(before, after)
    return {
        "mode": after["mode"],
        "qualifies": counted and not not_qualifying,
        "not_qualifying_because": not_qualifying,
        "outcome": "not_counted"
        if not counted
        else ("incident" if reasons else "retained"),
        "incident_reasons": reasons,
    }


def _incident_reasons(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    old, new = before["baseline"], after["snapshot"]
    if after["status"]["verdict"] != "valid":
        reasons.append(f"the after-status is {after['status']['verdict']}")
    if after["owner_wait"]["timed_out"]:
        reasons.append("an owner was still alive when the wait ended")
    if after["owner_log"]["state"] in {
        "missing",
        "moved",
        "replaced",
        "truncated",
        "rewritten",
        "refused",
    }:
        reasons.append(f"the owner log was {after['owner_log']['state']}")
    for store in ("source_state", "cookies", "profile"):
        if new[store]["state"] != "present":
            reasons.append(f"{store} is {new[store]['state']} afterwards")
    for field in ("login_generation", "source_runtime_id"):
        if new["source_state"].get(field) != old["source_state"].get(field):
            reasons.append(f"{field} changed")
    # A close may export the cookies again, so only the session cookie going
    # missing counts, never a different hash.
    if old["cookies"].get("li_at") and not new["cookies"].get("li_at"):
        reasons.append("the li_at cookie is gone")
    if (
        new["quarantines"]["state"] != "present"
        and old["quarantines"]["state"] == "present"
    ):
        reasons.append(f"the auth root is {new['quarantines']['state']} afterwards")
    added = set(new["quarantines"].get("names", [])) - set(
        old["quarantines"].get("names", [])
    )
    if added:
        reasons.append(f"new quarantine {', '.join(sorted(added))}")
    return reasons


# --- Commands ---------------------------------------------------------------


def _write_once(path: Path, record: dict[str, Any], auth_root: Path) -> None:
    text = json.dumps(record, indent=2, sort_keys=True) + "\n"
    _create(path, text.encode("utf-8"), auth_root)


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def before(args: argparse.Namespace) -> int:
    profile = canonical(Path(args.user_data_dir))
    auth_root = auth_root_dir(profile)
    _refuse_protected(args.pilot_dir, auth_root)
    if not args.others_stopped:
        raise RecorderError(
            "Stop every other MCP host first, then confirm it with --others-stopped."
        )
    if live := live_owners(auth_root):
        raise RecorderError(
            "An owner of this auth root may still be running, so this would not "
            f"be a cold start: {json.dumps(live)}. Wait for it to exit."
        )

    session = (
        f"{datetime.datetime.now(datetime.UTC):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
    )
    args.pilot_dir.mkdir(parents=True, exist_ok=True)
    folder = args.pilot_dir / session
    _refuse_protected(folder, auth_root)
    folder.mkdir()

    first = passive_snapshot(profile)
    status = run_status(args.candidate, profile)
    # After the status, which may export the cookies again and leaves a trace
    # run of its own that is not part of the session.
    baseline = passive_snapshot(profile)
    runs = trace_runs(auth_root)
    log = owner_log_position(profile)

    package = _version("mcp-server-linkedin")
    reasons = []
    if status["verdict"] != "valid":
        reasons.append(f"the before-status is {status['verdict']}")
    if status["package_version"] != package:
        reasons.append(
            f"the candidate runs {status['package_version']} and this "
            f"interpreter has {package}"
        )
    if "refused" in log:
        reasons.append(f"the owner log cannot be read: {log['refused']}")
    record = {
        "session": session,
        "recorded_at": _now(),
        "os": _os_name(),
        # Not `platform`, which asks WMI on Windows (docs/windows-wmi-crash.md).
        "runtime": get_runtime_id(),
        "host": args.host,
        "host_config": HOST_CONFIG,
        "candidate": args.candidate,
        "candidate_sha": candidate_sha(args.candidate),
        "interpreter": sys.executable,
        "versions": {"package": package, "patchright": _version("patchright")},
        "profile": str(profile),
        "auth_root": str(auth_root),
        "preflight": {"others_stopped": True, "note": _PREFLIGHT_NOTE},
        "snapshot_before_status": first,
        "status": status,
        "baseline": baseline,
        "trace_runs": runs,
        "owner_log": log,
        "counts": not reasons,
        "not_counted_because": reasons,
    }
    _write_once(folder / "before.json", record, auth_root)
    print(f"Session {session}")
    if reasons:
        print(f"This session does not count: {'; '.join(reasons)}.")
        return 1
    print("Start the host now. Run `after` once it has quit.")
    return 0


def _attestation(value: str) -> str:
    kind, _, when = value.partition("@")
    if kind not in _ATTESTED or not when:
        raise argparse.ArgumentTypeError("use login@TIME, logout@TIME or import@TIME")
    return value


def wait_for_owners(auth_root: Path) -> dict[str, Any]:
    """Wait for every owner of *auth_root* to exit. Never kills one."""
    deadline = time.monotonic() + _IDLE_WAIT_SECONDS
    started = time.monotonic()
    announced = False
    while live := live_owners(auth_root):
        if time.monotonic() >= deadline:
            return {
                "timed_out": True,
                "seconds": round(time.monotonic() - started),
                "live": live,
            }
        if not announced:
            print(
                f"Waiting up to {_IDLE_WAIT_SECONDS // 60} minutes for the owner to exit."
            )
            announced = True
        time.sleep(_POLL_SECONDS)
    return {"timed_out": False, "seconds": round(time.monotonic() - started)}


def _session_folder(pilot_dir: Path, session: str) -> tuple[Path, dict[str, Any]]:
    """The session folder and its ``before.json``, as the recorder left them.

    A link in place of the folder or of an entry in it, or any entry besides
    ``before.json`` and the capture folder, could aim a write somewhere else,
    so ``after`` refuses the folder before it writes anything. Nothing is
    ever written into an existing capture folder.
    """
    folder = pilot_dir / session
    if os.path.lexists(folder / "after.json"):
        raise RecorderError(f"Session {session} already has after.json")
    try:
        folder_mode = folder.lstat().st_mode
        before_mode = (folder / "before.json").lstat().st_mode
    except FileNotFoundError:
        raise RecorderError(f"No before.json for session {session}") from None
    if not stat.S_ISDIR(folder_mode) or not stat.S_ISREG(before_mode):
        raise RecorderError(
            f"{folder} or its before.json is a link or not what before wrote"
        )
    extra = sorted(
        name
        for name in os.listdir(folder)
        if name != "before.json"
        and not (name == _CAPTURE and stat.S_ISDIR((folder / name).lstat().st_mode))
    )
    if extra:
        raise RecorderError(
            f"{folder} holds {', '.join(extra)}, which the recorder did not write. "
            "Record a new session."
        )
    return folder, json.loads((folder / "before.json").read_text(encoding="utf-8"))


def capture_session(
    folder: Path, record_before: dict[str, Any], profile: Path, auth_root: Path
) -> dict[str, Any]:
    """Record what the host session left, once, before any active step.

    The stores, the owner log window and the trace runs go into a new
    capture folder, and ``capture.json`` is written last, so it exists only
    for a complete capture. The status check that follows may export the
    cookies again and leaves a trace run of its own, so a retried ``after``
    reuses this capture as it is and repeats only the status check.
    """
    owner_wait = wait_for_owners(auth_root)
    capture = folder / _CAPTURE
    capture.mkdir()
    snapshot = passive_snapshot(profile)
    owner_log, owner_lines = copy_owner_log(
        record_before["owner_log"], profile, capture, auth_root
    )
    runs = copy_trace_runs(auth_root, record_before["trace_runs"], capture, owner_lines)
    captured = {
        "captured_at": _now(),
        "owner_wait": owner_wait,
        "snapshot": snapshot,
        "owner_log": owner_log,
        "trace_runs": runs,
    }
    _write_once(capture / "capture.json", captured, auth_root)
    return captured


def _saved_capture(capture: Path) -> dict[str, Any] | None:
    """The capture an earlier ``after`` completed, or None.

    None also for a ``capture.json`` that is not the shape ``capture_session``
    writes, or whose runs and log disagree with the copies saved beside it:
    every saved copy has to be named and every named copy saved, so a capture
    can be neither shrunk nor emptied into a healthier session.
    """
    path = capture / "capture.json"
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        captured = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not _fits(captured, _CAPTURE_SHAPE):
        return None
    named = _named_copies(captured)
    try:
        saved = _saved_copies(capture)
    except OSError:
        return None
    if named is None or named != saved:
        return None
    return captured


class _Optional(NamedTuple):
    """A key that may be absent, and has *shape* when present."""

    shape: Any


_REASON = _Optional(str)
_COUNTS: dict[str, Any] = {"reading": int, "writing": int}
#: Every key ``capture_session`` writes into ``capture.json``, with its type:
#: a dict maps keys to shapes and allows no other key, a one-element list is
#: a list of that shape, a frozenset is the strings allowed, a tuple is any of
#: its types, and ``int`` excludes ``bool``.
_CAPTURE_SHAPE: dict[str, Any] = {
    "captured_at": str,
    "owner_wait": {
        "timed_out": bool,
        "seconds": int,
        "live": _Optional(
            [{"path": str, "pid": _Optional(int), "unreadable": _REASON}]
        ),
    },
    "snapshot": {
        "taken_at": str,
        "source_state": {
            "state": str,
            "reason": _REASON,
            "login_generation": _Optional(str),
            "source_runtime_id": _Optional(str),
        },
        "cookies": {
            "state": str,
            "reason": _REASON,
            "sha256": _Optional(str),
            "li_at": _Optional(bool),
        },
        "profile": {"state": str, "reason": _REASON},
        "last_version": {"state": str, "reason": _REASON, "value": _Optional(str)},
        "quarantines": {"state": str, "reason": _REASON, "names": _Optional([str])},
    },
    "owner_log": {
        "path": (str, type(None)),
        "state": str,
        "reason": _REASON,
        "tool_calls": _COUNTS,
        "from_offset": _Optional(int),
        "bytes": _Optional(int),
        "evidence": _Optional(str),
        "records": _Optional(int),
        "non_json_lines": _Optional(int),
    },
    "trace_runs": [
        {
            "name": str,
            "kind": frozenset({"frontend", "owner", "profile_command", "unknown"}),
            "mode": _Optional(frozenset({"daemon", "fallback", "uncertain"})),
            "command": _Optional(str),
            "tool_calls": _Optional(_COUNTS),
            "evidence": _Optional(str),
            "refused": _REASON,
            "missing": _REASON,
        }
    ],
}


def _fits(value: Any, shape: Any) -> bool:
    """Whether *value* has *shape*, as ``_CAPTURE_SHAPE`` spells shapes."""
    if isinstance(shape, dict):
        return (
            isinstance(value, dict)
            and value.keys() <= shape.keys()
            and all(
                (key not in value or _fits(value[key], sub.shape))
                if isinstance(sub, _Optional)
                else (key in value and _fits(value[key], sub))
                for key, sub in shape.items()
            )
        )
    if isinstance(shape, list):
        return isinstance(value, list) and all(_fits(item, shape[0]) for item in value)
    if isinstance(shape, frozenset):
        return isinstance(value, str) and value in shape
    if shape is int:
        return type(value) is int
    return isinstance(value, shape)


def _named_copies(captured: dict[str, Any]) -> set[str] | None:
    """The copies a capture of the right shape names, or None if its runs
    contradict how ``copy_trace_runs`` writes them."""
    log = captured["owner_log"]
    if log.get("evidence", "daemon.log") != "daemon.log":
        return None
    named = {"daemon.log"} if "evidence" in log else set()
    for run in captured["trace_runs"]:
        if run["kind"] == "frontend" and "mode" not in run:
            return None
        if "evidence" not in run:
            if run["kind"] != "unknown":
                return None
            continue
        evidence = f"trace-runs/{run['name']}/server.log"
        if run["evidence"] != evidence or evidence in named or "tool_calls" not in run:
            return None
        named.add(evidence)
    return named


def _saved_copies(capture: Path) -> set[str] | None:
    """Every file saved under *capture* but the manifest, or None for a link
    or anything else that is neither a plain directory nor a regular file."""
    saved: set[str] = set()
    for directory, folders, files in os.walk(capture):
        for name in [*folders, *files]:
            mode = (Path(directory) / name).lstat().st_mode
            if name in files and stat.S_ISREG(mode):
                saved.add((Path(directory) / name).relative_to(capture).as_posix())
            elif not (name in folders and stat.S_ISDIR(mode)):
                return None
    saved.discard("capture.json")
    return saved


def _interrupted_capture(
    session: str, record_before: dict[str, Any], attestations: list[str]
) -> dict[str, Any]:
    """The record of a session whose capture was cut short or does not hold
    together: never retained, and never captured again."""
    counted = bool(record_before["counts"])
    return {
        "session": session,
        "recorded_at": _now(),
        "attestations": attestations,
        "evidence": _CAPTURE,
        "capture_complete": False,
        "judgement": {
            "mode": "uncertain",
            "qualifies": False,
            "not_qualifying_because": [
                *record_before["not_counted_because"],
                "the capture is incomplete or inconsistent",
            ],
            "outcome": "incident" if counted else "not_counted",
            "incident_reasons": (
                ["the capture is incomplete or inconsistent"] if counted else []
            ),
        },
    }


def after(args: argparse.Namespace) -> int:
    if not _SESSION_ID.fullmatch(args.session):
        raise RecorderError(f"{args.session!r} is not a session ID")
    folder, record_before = _session_folder(args.pilot_dir, args.session)
    profile = Path(record_before["profile"])
    auth_root = Path(record_before["auth_root"])
    _refuse_protected(folder, auth_root)

    capture = folder / _CAPTURE
    if not os.path.lexists(capture):
        captured = capture_session(folder, record_before, profile, auth_root)
    elif (captured := _saved_capture(capture)) is None:
        record = _interrupted_capture(args.session, record_before, args.attest)
        _write_once(folder / "after.json", record, auth_root)
        return _report(record)
    # Frontends come only from the capture, which a retry has checked
    # against its saved copies.
    status = run_status(record_before["candidate"], profile)
    record: dict[str, Any] = {
        "session": args.session,
        "recorded_at": _now(),
        "attestations": args.attest,
        "evidence": _CAPTURE,
        "capture_complete": True,
        # Named one by one, so a capture can never set a field of after's own.
        "captured_at": captured["captured_at"],
        "owner_wait": captured["owner_wait"],
        "snapshot": captured["snapshot"],
        "owner_log": captured["owner_log"],
        "trace_runs": captured["trace_runs"],
        "mode": session_mode(captured["trace_runs"]),
        "status": status,
    }
    record["judgement"] = judge(record_before, record)
    _write_once(folder / "after.json", record, auth_root)
    return _report(record)


def _report(record: dict[str, Any]) -> int:
    verdict = record["judgement"]
    print(
        f"Session {record['session']}: mode {verdict['mode']}, outcome "
        f"{verdict['outcome']}, qualifies {verdict['qualifies']}."
    )
    for reason in verdict["incident_reasons"]:
        print(f"  incident: {reason}")
    for reason in verdict["not_qualifying_because"]:
        print(f"  does not qualify: {reason}")
    return 0


def candidate_sha(candidate: str) -> str | None:
    """The commit a ``--from git+...@SHA`` candidate installs, if it names one."""
    found = _CANDIDATE_SHA.search(candidate)
    return found.group(1).lower() if found else None


def tally(args: argparse.Namespace) -> int:
    """Count one candidate's sessions against the quotas.

    Non-zero until every quota is met with no incident or uncertain session.
    Sessions of any other candidate are left out, so a reused directory
    cannot fill one build's quotas with another's sessions.
    """
    sessions: dict[str, tuple[Path, dict[str, Any], dict[str, Any] | None]] = {}
    other = 0
    for pilot_dir in args.pilot_dirs:
        for path in sorted(Path(pilot_dir).glob("*/before.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("candidate_sha") != args.sha.lower():
                other += 1
                continue
            if record["session"] in sessions:
                print(
                    f"{record['session']} appears again in {path.parent}; counted once"
                )
                continue
            after_path = path.parent / "after.json"
            later = (
                json.loads(after_path.read_text(encoding="utf-8"))
                if after_path.exists()
                else None
            )
            sessions[record["session"]] = (path.parent, record, later)

    rows = {name: {"daemon": 0, "fallback": 0, "uncertain": 0} for name in QUOTAS}
    incidents: list[str] = []
    uncertain: list[str] = []
    for session, (folder, record, later) in sorted(sessions.items()):
        if later is None:
            # A start that did not count is left there by design; one that
            # could count and was never finished may hide a loss.
            if record["counts"]:
                incidents.append(f"{session} ({folder}): after was never recorded")
            continue
        verdict = later["judgement"]
        if verdict["outcome"] == "incident":
            incidents.append(
                f"{session} ({folder}): {'; '.join(verdict['incident_reasons'])}"
            )
        row = rows.setdefault(
            record["os"], {"daemon": 0, "fallback": 0, "uncertain": 0}
        )
        # Before the qualifying check: an uncertain session may show no read
        # at all, and must still block.
        if verdict["mode"] == "uncertain" and verdict["outcome"] != "not_counted":
            row["uncertain"] += 1
            uncertain.append(f"{session} ({folder})")
            continue
        if not verdict["qualifies"]:
            continue
        if verdict["mode"] == "fallback":
            row["fallback"] += 1
        elif verdict["outcome"] == "retained":
            row["daemon"] += 1

    if other:
        print(f"{other} session(s) of other candidates left out")
    unmet = False
    print("OS        daemon/quota  fallback  uncertain")
    for name, row in rows.items():
        quota = QUOTAS.get(name, 0)
        unmet |= row["daemon"] < quota
        print(
            f"{name:<9} {row['daemon']:>6}/{quota:<6} {row['fallback']:>8}  {row['uncertain']:>9}"
        )
    for line in incidents:
        print(f"incident: {line}")
    for line in uncertain:
        print(f"uncertain: {line}")
    return 1 if unmet or incidents or uncertain else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    start = commands.add_parser(
        "before", help="record the session before the host starts"
    )
    start.add_argument("--pilot-dir", type=Path, required=True)
    start.add_argument(
        "--candidate", required=True, help="the uvx command prefix that runs the server"
    )
    start.add_argument(
        "--host", required=True, help="the MCP host, e.g. claude-desktop"
    )
    start.add_argument(
        "--others-stopped",
        action="store_true",
        help="confirm that every other host is stopped",
    )
    start.add_argument(
        "--user-data-dir",
        default=os.environ.get("USER_DATA_DIR") or "~/.linkedin-mcp/profile",
        help="the profile the host uses (default: USER_DATA_DIR or ~/.linkedin-mcp/profile)",
    )
    start.set_defaults(run=before)

    end = commands.add_parser("after", help="record the session once the host has quit")
    end.add_argument("--pilot-dir", type=Path, required=True)
    end.add_argument("--session", required=True)
    end.add_argument(
        "--attest",
        type=_attestation,
        action="append",
        default=[],
        help="a login, logout or import the tester ran, as KIND@TIME",
    )
    end.set_defaults(run=after)

    count = commands.add_parser("tally", help="count sessions against the quotas")
    count.add_argument(
        "--sha", required=True, help="the full candidate commit to count"
    )
    count.add_argument("pilot_dirs", nargs="+", type=Path)
    count.set_defaults(run=tally)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except RecorderError as exc:
        print(f"Refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
