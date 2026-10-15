"""Record one pilot session of the shared-browser daemon (release gate 3).

Run with the candidate's own interpreter, as ``docs/daemon-pilot.md`` shows:

    python daemon_pilot.py before --pilot-dir D --host NAME --others-stopped \\
        --candidate "uvx --isolated --cache-dir C --from git+...@SHA mcp-server-linkedin"
    # start the host, read LinkedIn through it, quit the host
    python daemon_pilot.py after --pilot-dir D --session ID [--attest login@TIME]
    python daemon_pilot.py tally --sha SHA D [D2 ...]

How a session is read is decided in
``docs/decisions/2026-10-15-daemon-gates-2-and-3-procedure.md``.

Read-only towards the session: nothing is written under the auth root, no
cookie value is stored, and no helper that prepares, claims, locks or prunes is
imported. The only writes go to the pilot directory, which may not sit inside
the auth root. ``--status`` is the one active step, run through the candidate.
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
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from linkedin_mcp_server.daemon.descriptor import daemon_state_root, profile_identity
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
_STATUS_OUTPUT_CHARS = 20_000
_DESCRIPTOR_BYTES = 64 * 1024
_SESSION_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")
_ATTESTED = ("login", "logout", "import")
_CANDIDATE_SHA = re.compile(r"git\+\S+@([0-9a-fA-F]{40})(?![0-9a-zA-Z])")

# The CLI's own lines. Every process `main` starts logs the banner; a profile
# command logs it again with its mode, so a banner alone is a server frontend.
_BANNER = re.compile(r"LinkedIn MCP Server v\S+")
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


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _os_name() -> str:
    return {"darwin": "macos", "win32": "windows"}.get(sys.platform, sys.platform)


def _inside(path: Path, root: Path) -> bool:
    """Whether *path* is *root* or below it, symlinks resolved."""
    child = Path(os.path.normcase(canonical(path)))
    parent = Path(os.path.normcase(canonical(root)))
    return child == parent or parent in child.parents


def _refuse_inside_auth_root(pilot_dir: Path, auth_root: Path) -> None:
    if _inside(pilot_dir, auth_root):
        raise RecorderError(
            f"The pilot directory {pilot_dir} is inside the auth root {auth_root}. "
            "Choose a directory outside it."
        )


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


def _command(candidate: str, extra: Sequence[str]) -> str | list[str]:
    # A Windows command line is a string CreateProcess parses itself, and
    # shlex would eat the backslashes of a Windows path.
    if os.name == "nt":
        return f"{candidate} {subprocess.list2cmdline(extra)}"
    return [*shlex.split(candidate), *extra]


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def run_status(candidate: str, profile: Path) -> dict[str, Any]:
    """Run ``--status`` through the candidate and keep its bounded output."""
    extra = ["--status", "--log-level", "INFO", "--user-data-dir", str(profile)]
    command = _command(candidate, extra)
    started = _now()
    exit_code: int | None
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_STATUS_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        exit_code, output = None, f"{_text(exc.stdout)}{_text(exc.stderr)}\n[timed out]"
    except OSError as exc:
        exit_code, output = None, f"[could not start: {exc}]"
    else:
        exit_code, output = done.returncode, _text(done.stdout) + _text(done.stderr)
    version = _STATUS_VERSION.search(output)
    return {
        "started_at": started,
        "command": command,
        "exit": exit_code,
        "verdict": classify_status(exit_code, output),
        "package_version": version.group(1) if version else None,
        "output": output[-_STATUS_OUTPUT_CHARS:],
        "output_truncated": len(output) > _STATUS_OUTPUT_CHARS,
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


def owner_log_position(profile: Path) -> dict[str, Any]:
    """Where the owner log of *profile* ends now, and which file that is."""
    path = _owner_log(profile)
    if path is None:
        return {"path": None}
    try:
        info = path.stat()
    except FileNotFoundError:
        return {"path": str(path), "identity": None, "offset": 0}
    return {
        "path": str(path),
        "identity": [info.st_dev, info.st_ino],
        "offset": info.st_size,
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
    before: dict[str, Any], profile: Path, folder: Path
) -> dict[str, Any]:
    """Copy the part of the owner log written since ``before``, and judge the file.

    A different file at that path, a shorter one, or a log in another
    directory each mean the window cannot be trusted.
    """
    now = owner_log_position(profile)
    result: dict[str, Any] = {"path": now["path"], "state": "absent"}
    if now["path"] is None or now.get("identity") is None:
        if before.get("identity") is not None:
            result["state"] = "missing"
        result["tool_calls"] = {"reading": 0, "writing": 0}
        return result
    offset = 0
    if before["path"] is None or before.get("identity") is None:
        result["state"] = "created"
    elif before["path"] != now["path"]:
        result["state"] = "moved"
    elif before["identity"] != now["identity"]:
        result["state"] = "replaced"
    elif now["offset"] < before["offset"]:
        result["state"] = "truncated"
    else:
        result["state"] = "continued"
        offset = before["offset"]
    with open(now["path"], "rb") as handle:
        handle.seek(offset)
        window = handle.read()
    (folder / "daemon.log").write_bytes(window)
    records, other = read_log_records(window.decode("utf-8", errors="replace"))
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
    return result


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


def classify_run(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Say what process wrote one trace run, and a frontend's startup decision.

    The banner and the forwarding line are INFO, so a frontend logging below
    that has no banner: a fallback warning still names it a frontend, and a
    run with no record at all is ``unknown``, which makes its session
    uncertain. A run with records but no banner is unattributed (an owner)
    and is listed, never dropped.
    """
    banner = False
    mode: Mode | None = None
    seen = False
    for record in records:
        seen = True
        message = record["message"]
        if command := _COMMAND.fullmatch(message):
            return {"kind": "profile_command", "command": command.group(1)}
        if _BANNER.fullmatch(message):
            banner = True
        elif mode is None:
            mode = _decision(message)
    if banner or mode is not None:
        return {"kind": "frontend", "mode": mode or "uncertain"}
    return {"kind": "unattributed" if seen else "unknown"}


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
    auth_root: Path, baseline: Iterable[str], folder: Path
) -> list[dict[str, Any]]:
    """Copy each trace run created since the baseline, and classify it."""
    known = set(baseline)
    runs: list[dict[str, Any]] = []
    for name in trace_runs(auth_root):
        if name in known:
            continue
        evidence = Path("trace-runs") / name / "server.log"
        target = folder / evidence
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(auth_root / "trace-runs" / name / "server.log", target)
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            runs.append(
                {"name": name, "kind": "unknown", "missing": type(exc).__name__}
            )
            continue
        records, _ = read_log_records(text)
        runs.append(
            {
                "name": name,
                **classify_run(records),
                "tool_calls": tool_calls(records),
                "evidence": evidence.as_posix(),
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
            run.get("tool_calls", {}).get("reading", 0) for run in after["trace_runs"]
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
    if after["owner_log"]["state"] in {"missing", "moved", "replaced", "truncated"}:
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


def _write_once(path: Path, record: dict[str, Any]) -> None:
    with open(path, "x", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def before(args: argparse.Namespace) -> int:
    profile = canonical(Path(args.user_data_dir))
    auth_root = auth_root_dir(profile)
    _refuse_inside_auth_root(args.pilot_dir, auth_root)
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
    folder = args.pilot_dir / session
    folder.mkdir(parents=True)

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
    _write_once(folder / "before.json", record)
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


def after(args: argparse.Namespace) -> int:
    if not _SESSION_ID.fullmatch(args.session):
        raise RecorderError(f"{args.session!r} is not a session ID")
    folder = args.pilot_dir / args.session
    try:
        record_before = json.loads((folder / "before.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecorderError(f"No before.json for session {args.session}") from None
    if (folder / "after.json").exists():
        raise RecorderError(f"Session {args.session} already has after.json")
    profile = Path(record_before["profile"])
    auth_root = Path(record_before["auth_root"])
    _refuse_inside_auth_root(args.pilot_dir, auth_root)

    owner_wait = wait_for_owners(auth_root)
    owner_log = copy_owner_log(record_before["owner_log"], profile, folder)
    runs = copy_trace_runs(auth_root, record_before["trace_runs"], folder)
    snapshot = passive_snapshot(profile)
    status = run_status(record_before["candidate"], profile)
    record: dict[str, Any] = {
        "session": args.session,
        "recorded_at": _now(),
        "attestations": args.attest,
        "owner_wait": owner_wait,
        "owner_log": owner_log,
        "trace_runs": runs,
        "mode": session_mode(runs),
        "snapshot": snapshot,
        "status": status,
    }
    record["judgement"] = judge(record_before, record)
    _write_once(folder / "after.json", record)

    verdict = record["judgement"]
    print(
        f"Session {args.session}: mode {verdict['mode']}, outcome "
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
        # Before the qualifying check: a host started without INFO logging
        # shows neither a decision nor a read, and must still block the gate.
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
