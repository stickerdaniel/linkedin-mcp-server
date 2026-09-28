"""Row H-R11's planted fault: one failed Job-membership query, and a Job member.

**The shim.** A declared, row-scoped ``sitecustomize`` that fails exactly one
dependency call: ``win32job.IsProcessInJob`` when, and only when, the frame
calling it is ``linkedin_mcp_server.process_tree._in_another_owned_job``. That
is the query the routine drain asks of a member of the owner's adopted Job:
whether it also sits in another Job the owner holds. The handle it asks about
alone cannot single the call out, because the installer's own assignment
check (``WindowsJob._assign_handle``) asks the same Job about the same
process, and failing that one would stop the installer from starting at all.
The failure raised is the one the real API raises, ``pywintypes.error``. Every
time the planted failure fires it appends a line to ``h-r11-reached.jsonl``
beside the shim (the calling pid, the member asked about and its creation
time, the Job handle), so
a row can say whether its query was reached. Nothing else in any process is
changed. Planting it inside an actor is Daniel's amendment to the plan
(FABLE_PLAN_V7, 2026-09-27): the baseline cannot gain a production seam, and
closing the owner's handle from outside would alter its handle table and keep
the installer's Job alive.

**The observer.** The same shim wraps ``win32api.TerminateProcess`` and changes
nothing about it: the real call runs exactly once with the arguments it was
given, and its result or error is the caller's. A call from either drain in
``process_tree`` (``_drain_adopted_windows_job_members``, the routine one, or
``_drain_adopted_windows_job``, the baseline's hard exit) is recorded with the
caller's name, the pid and creation time of the process the handle names, and
whether the call succeeded. That record is what tells the routine drain's
termination from shared setup shutdown's ``TerminateJobObject``: both end the
installer with code 1, and no time the harness can read separates them (review
e1en, E1EN-02; declared as an amendment to FABLE_PLAN_V7, 2026-09-28).

**A missing record is not no record.** Every process the shim starts in
writes a ``ready`` record first, with its creation time and whether the fault
and the observer are both in place, and numbers every record it writes; one
it could not write keeps its number, so the gap shows, and is also announced
on stderr. A termination is recorded when it begins and again when it ends.
The harness reads the closing owner's records only once they are shown
complete (``shim_log``); otherwise the drain's reading is unknown (review
e1ep, E1EP-02).

**Where it lives.** The owner is started from ``sys.executable`` with ``-P``,
so the only thing that reaches it is the interpreter's own startup. The shim
therefore sits in a venv of its own (``make_shim_venv``), made with
``venv --without-pip`` from the source venv's base interpreter, holding two
files and nothing else: the shim, and ``_h_r11_code.pth``, which adds the
source venv's ``site-packages`` with ``site.addsitedir`` so every import,
the product's included, resolves to the source venv's code. The same shim text
goes into the K1, K2 and K3 venvs; its SHA-256 and the ``.pth``'s are recorded,
and the venv is checked to import the product from exactly the file and
``direct_url.json`` the source venv does.

**The member.** At a routine close the adopted Job normally holds nobody but
the owner, so ``_in_another_owned_job`` is never asked. What the plan's R11
names is the installer, which sits in its own Job *and* in the adopted one. It
runs whenever setup finds the browser not ready. The row keeps it running
without a product seam: the actors' browser cache is a row-private directory
of links to the runtime's installed browser and its dependencies
(``PrivateCache``), and after the first read the row holds one dependency back
(``winldd`` on Windows) and drops the row's own install metadata, so the next
call starts setup, patchright finds the dependency missing and downloads it
from ``PLAYWRIGHT_DOWNLOAD_HOST``, which is a loopback host that accepts and
never answers (``StallHost``). The browser the row runs is never touched: only
the row-private links are.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The one caller whose ``IsProcessInJob`` fails.
SHIMMED_FUNCTION = "_in_another_owned_job"
SHIMMED_MODULE = "linkedin_mcp_server.process_tree"
REACHED_FILE = "h-r11-reached.jsonl"
PTH_FILE = "_h_r11_code.pth"

SHIM_SOURCE = '''\
"""H-R11's declared shim: fails one IsProcessInJob call, and records it.

See tests/differential/job_query.py. Fails the call only when it is made from
linkedin_mcp_server.process_tree._in_another_owned_job; every other call goes
to the real API unchanged. It also observes, and changes nothing about, the
TerminateProcess calls the two process_tree drains make.
"""

import itertools
import json
import os
import sys
import time

_RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "h-r11-reached.jsonl")
_MODULE = "linkedin_mcp_server.process_tree"
_DRAINS = ("_drain_adopted_windows_job_members", "_drain_adopted_windows_job")
_LOST = "h-r11 shim: record lost"
#: This process's records: a token of its own, and numbers with no gap unless
#: a record was lost.
_TOKEN = os.urandom(8).hex()
_NUMBERS = itertools.count(1)
_CALLS = itertools.count(1)


def _write(record, line):
    """Append *line*, numbered; a record that could not be written keeps its
    number, so the gap shows, and is announced on stderr as well."""
    number = next(_NUMBERS)
    try:
        text = json.dumps(dict(line, pid=os.getpid(), token=_TOKEN, seq=number))
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(text + "\\n")
    except Exception as exc:
        try:
            sys.stderr.write(f"{_LOST} {os.getpid()} {number}: {exc!r}\\n")
            sys.stderr.flush()
        except Exception:
            pass


def _member(identity, handle):
    try:
        member, created = identity(handle)
    except Exception:
        return None, None
    return member, created


def install(win32job, error, identity, record=_RECORD):
    """Wrap win32job.IsProcessInJob; the doubles in the tests call this too."""
    real = win32job.IsProcessInJob

    def IsProcessInJob(process, job):
        caller = sys._getframe(1)
        if (
            caller.f_code.co_name == "_in_another_owned_job"
            and caller.f_globals.get("__name__") == _MODULE
        ):
            member, created = _member(identity, process)
            try:
                handle = int(job)
            except Exception:
                handle = None
            _write(record, {"kind": "query", "t": time.time(), "member": member,
                            "created": created, "job": handle})
            raise error(5, "IsProcessInJob", "planted by the H-R11 shim")
        return real(process, job)

    win32job.IsProcessInJob = IsProcessInJob


def observe(win32api, identity, record=_RECORD):
    """Wrap win32api.TerminateProcess to record the drains' calls, and no more.

    The real API is called exactly once, with the arguments it was given, and
    its result or its error goes back to the caller unchanged; nothing the
    recording does can raise into the caller. A call from one of the two
    drains in process_tree is recorded when it begins, with that caller's
    name and the member the handle names, and again when it ends, with
    whether it succeeded.
    """
    real = win32api.TerminateProcess

    def TerminateProcess(*args, **kwargs):
        caller = sys._getframe(1)
        name = caller.f_code.co_name
        if caller.f_globals.get("__name__") != _MODULE or name not in _DRAINS:
            return real(*args, **kwargs)
        member, created = _member(identity, args[0] if args else None)
        call = next(_CALLS)
        _write(record, {"kind": "terminate", "phase": "begin", "call": call,
                        "caller": name, "member": member, "created": created,
                        "t": time.time()})
        try:
            result = real(*args, **kwargs)
        except BaseException as exc:
            _write(record, {"kind": "terminate", "phase": "end", "call": call,
                            "succeeded": False, "error": repr(exc), "t": time.time()})
            raise
        _write(record, {"kind": "terminate", "phase": "end", "call": call,
                        "succeeded": True, "t": time.time()})
        return result

    win32api.TerminateProcess = TerminateProcess


def ready(record=_RECORD, *, created=None, fault=False, observer=False):
    """This process's first record: which parts are in place, and its lifetime."""
    _write(record, {"kind": "ready", "created": created, "fault": fault,
                    "observer": observer, "t": time.time()})


def _times(handle):
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(int(handle)), *(ctypes.byref(t) for t in times)
    ):
        return None
    ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return (ticks - 116444736000000000) / 10000000


def _identity(handle):
    """The pid a process handle names, and that process's creation time."""
    import win32process

    return win32process.GetProcessId(handle), _times(handle)


if sys.platform == "win32" and __name__ == "sitecustomize":
    _parts = {"fault": False, "observer": False}
    try:
        import pywintypes
        import win32job

        install(win32job, pywintypes.error, _identity)
        _parts["fault"] = True
    except Exception:
        pass
    try:
        import win32api

        observe(win32api, _identity)
        _parts["observer"] = True
    except Exception:
        pass
    try:
        import win32api

        _self = _times(win32api.GetCurrentProcess())
    except Exception:
        _self = None
    ready(created=_self, **_parts)
'''


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


SHIM_SHA256 = _sha256(SHIM_SOURCE)


def pth_line(site_packages: str) -> str:
    """The ``.pth`` line that puts the source venv's code on the path."""
    return f"import site; site.addsitedir({site_packages!r})\n"


def shim_namespace() -> dict[str, Any]:
    """Expose the shim's installer for doubles without patching the host process."""
    namespace: dict[str, Any] = {"__name__": "h_r11_model", "__file__": "shim"}
    exec(compile(SHIM_SOURCE, "sitecustomize.py", "exec"), namespace)
    return namespace


def venv_interpreter(directory: Path) -> Path:
    if sys.platform == "win32":
        return directory / "Scripts" / "python.exe"
    return directory / "bin" / "python"


_ASK_SOURCE = """
import json, sys, sysconfig
print(json.dumps({"base": getattr(sys, "_base_executable", sys.executable),
                  "purelib": sysconfig.get_paths()["purelib"]}))
"""

_ASK_CODE = """
import json, sys
from importlib import metadata
import linkedin_mcp_server
dist = metadata.distribution("mcp-server-linkedin")
print(json.dumps({
    "module": linkedin_mcp_server.__file__,
    "direct_url": json.loads(dist.read_text("direct_url.json") or "null"),
    "version": dist.version,
    "sitecustomize": getattr(sys.modules.get("sitecustomize"), "__file__", None),
}))
"""


def _ask(python: str, program: str) -> dict[str, Any]:
    result = subprocess.run(
        [python, "-I", "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"asking {python} failed ({result.returncode}): {result.stderr[-2000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


@dataclass(frozen=True)
class ShimVenv:
    """The venv the row's actors start from, and what it was checked to be."""

    directory: Path
    python: str
    source_python: str
    site_packages: str
    shim_sha256: str
    pth_sha256: str
    #: What the source venv and this one import: must be equal but for the shim.
    source_code: dict[str, Any]
    code: dict[str, Any]

    @property
    def reached_file(self) -> Path:
        return Path(self.site_packages) / REACHED_FILE

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "shim_venv": str(self.directory),
            "shim_python": self.python,
            "source_python": self.source_python,
            "shim_sha256": self.shim_sha256,
            "pth_sha256": self.pth_sha256,
            "imports": self.code.get("module"),
            "direct_url": self.code.get("direct_url"),
            "sitecustomize": self.code.get("sitecustomize"),
        }


def code_difference(
    source: dict[str, Any], shimmed: dict[str, Any], shim_path: Path
) -> list[str]:
    """What the shim venv imports differently from its source, besides the shim."""
    problems = []
    for name in ("module", "direct_url", "version"):
        if source.get(name) != shimmed.get(name):
            problems.append(
                f"{name}: the source venv has {source.get(name)!r}, the shim venv "
                f"{shimmed.get(name)!r}"
            )
    actual = shimmed.get("sitecustomize")
    if not actual or Path(actual).resolve() != shim_path.resolve():
        problems.append(f"the shim venv did not run {shim_path}")
    return problems


def make_shim_venv(source_python: str, directory: Path) -> ShimVenv:
    """A venv at *directory* that runs *source_python*'s code plus the shim.

    Refuses, rather than returning, when the new venv imports the product
    from anywhere but the file and install record the source venv does.
    """
    source = _ask(source_python, _ASK_SOURCE)
    source_code = _ask(source_python, _ASK_CODE)
    made = subprocess.run(
        [source["base"], "-m", "venv", "--without-pip", str(directory)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if made.returncode != 0:
        raise RuntimeError(f"venv failed: {made.stderr[-2000:]}")
    python = str(venv_interpreter(directory))
    site_packages = _ask(python, _ASK_SOURCE)["purelib"]
    pth = pth_line(source["purelib"])
    Path(site_packages, PTH_FILE).write_text(pth, encoding="utf-8")
    shim_path = Path(site_packages, "sitecustomize.py")
    shim_path.write_text(SHIM_SOURCE, encoding="utf-8")
    code = _ask(python, _ASK_CODE)
    problems = code_difference(source_code, code, shim_path)
    if shim_path.read_text(encoding="utf-8") != SHIM_SOURCE:
        problems.append("the shim venv did not keep the declared shim source")
    if problems:
        raise RuntimeError(f"the shim venv does not run the source's code: {problems}")
    return ShimVenv(
        directory=directory,
        python=python,
        source_python=source_python,
        site_packages=site_packages,
        shim_sha256=SHIM_SHA256,
        pth_sha256=_sha256(pth),
        source_code=source_code,
        code=code,
    )


#: What the shim writes to stderr, the owner's log in daemon mode, when a
#: record could not be written.
LOST_MARKER = "h-r11 shim: record lost"

#: How far apart two readings of one creation time may be (``Fate.is_lifetime``).
_READY_TOLERANCE_SECONDS = 0.01


def _parsed(path: Path) -> tuple[list[dict[str, Any]], int]:
    """Every record at *path*, and how many lines could not be read as one."""
    if not path.is_file():
        return [], 0
    lines, unreadable = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            unreadable += 1
            continue
        if isinstance(entry, dict):
            lines.append(entry)
        else:
            unreadable += 1
    return lines, unreadable


def _joined(lines: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Each observed ``TerminateProcess`` call, its begin and end joined."""
    begun: dict[tuple[Any, Any], dict[str, Any]] = {}
    ends: dict[tuple[Any, Any], dict[str, Any]] = {}
    problems = []
    for line in lines:
        if line.get("kind") != "terminate":
            continue
        key = (line.get("token"), line.get("call"))
        if line.get("phase") == "begin":
            begun[key] = line
        elif line.get("phase") == "end":
            ends[key] = line
    calls = []
    for key, begin in begun.items():
        end = ends.get(key)
        calls.append(
            {
                "pid": begin.get("pid"),
                "caller": begin.get("caller"),
                "member": begin.get("member"),
                "created": begin.get("created"),
                "began": begin.get("t"),
                "ended": end.get("t") if end else None,
                # None: it began and no end was recorded, so it may have run.
                "succeeded": end.get("succeeded") if end else None,
                "error": end.get("error") if end else None,
            }
        )
    for key in ends.keys() - begun.keys():
        problems.append(f"a termination ended with no record of its start: {key}")
    return calls, problems


def reached(path: Path, pid: int | None = None) -> list[dict[str, Any]]:
    """The planted failures recorded at *path*, for *pid* when it is given."""
    lines, _ = _parsed(path)
    return [
        line
        for line in lines
        if line.get("kind") == "query" and (pid is None or line.get("pid") == pid)
    ]


def terminations(path: Path, pid: int | None = None) -> list[dict[str, Any]]:
    """The drains' ``TerminateProcess`` calls the shim observed, for *pid*."""
    lines, _ = _parsed(path)
    calls, _ = _joined(line for line in lines if pid is None or line.get("pid") == pid)
    return calls


@dataclass
class ShimLog:
    """One actor's records, and why they might not be all it made."""

    queries: list[dict[str, Any]] = field(default_factory=list)
    terminations: list[dict[str, Any]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def shim_log(
    path: Path,
    *,
    pid: int | None,
    created: float | None,
    lost: Iterable[str] = (),
) -> ShimLog:
    """The records of the process (*pid*, *created*), checked for completeness.

    Complete means: that lifetime wrote its ``ready`` record with the fault
    and the observer both in place; its records are numbered without a gap
    (a record that could not be written keeps its number); every line of the
    file reads as a record; no termination ended without a recorded start;
    and no line in *lost* (the actor's stderr, the owner's log) says one of
    its records was lost. Anything short of that is a problem, and a problem
    leaves the drain's reading unknown: an empty set of terminations is only
    evidence of none when nothing could have gone missing.

    With no *pid* (Direct, which has no adopted Job to drain) every record
    counts and only the file's own readability and lost lines are checked.
    """
    lines, unreadable = _parsed(path)
    log = ShimLog()
    if unreadable:
        log.problems.append(f"{unreadable} line(s) of the shim's record are unreadable")
    for line in lost:
        if LOST_MARKER in line and (pid is None or f"{LOST_MARKER} {pid} " in line):
            log.problems.append(f"the shim lost a record: {line.strip()[:200]}")
    if pid is None:
        mine = lines
    else:
        readies = [
            line
            for line in lines
            if line.get("kind") == "ready"
            and line.get("pid") == pid
            and isinstance(line.get("created"), (int, float))
            and created is not None
            and abs(float(line["created"]) - created) <= _READY_TOLERANCE_SECONDS
        ]
        if len(readies) != 1:
            log.problems.append(
                f"pid {pid} created {created} left {len(readies)} ready records, "
                f"so its shim is not shown in place"
            )
            return log
        (ready,) = readies
        if not (ready.get("fault") and ready.get("observer")):
            log.problems.append(
                f"pid {pid}'s shim was not all in place: fault={ready.get('fault')}, "
                f"observer={ready.get('observer')}"
            )
        mine = [line for line in lines if line.get("token") == ready.get("token")]
        numbers = sorted(
            line["seq"] for line in mine if isinstance(line.get("seq"), int)
        )
        if numbers != list(range(1, len(numbers) + 1)) or len(numbers) != len(mine):
            log.problems.append(
                f"pid {pid}'s records are not numbered 1 to {len(mine)} without "
                f"a gap: a record was lost"
            )
    log.queries = [line for line in mine if line.get("kind") == "query"]
    log.terminations, problems = _joined(mine)
    log.problems += problems
    return log


# --- The member: a row-private browser cache and a download that never ends ----


def install_locations(python: str, browsers: Path) -> list[Path]:
    """What ``patchright install chromium --no-shell`` would put in *browsers*.

    Read from the runtime's own ``--dry-run``, so platform-specific revisions
    and the Windows-only ``winldd`` come out as that patchright computes them.
    """
    result = subprocess.run(
        [python, "-m", "patchright", "install", "--dry-run", "chromium", "--no-shell"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env={**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)},
    )
    if result.returncode != 0:
        raise RuntimeError(f"install --dry-run failed: {result.stderr[-2000:]}")
    return [
        Path(line.split(":", 1)[1].strip())
        for line in result.stdout.splitlines()
        if line.strip().startswith("Install location:")
    ]


_RECORD_INSTALL = """
from linkedin_mcp_server import bootstrap
browsers = bootstrap.configure_browser_environment()
bootstrap._write_install_metadata(
    browsers, {bootstrap._SHELL_DIR_PREFIX: False, bootstrap._FULL_DIR_PREFIX: True}
)
print(bootstrap.browser_ready())
"""


def record_install(python: str, env: dict[str, str]) -> None:
    """Record the browser install for the cache *env* names, as setup would.

    Staging records the runtime's real cache, and the readiness check refuses
    a record whose ``browsers_path`` is not the configured one
    (``bootstrap._metadata_shape_ok``), so with the row-private cache
    configured the first call read "setup in progress" and ran no browser.
    Written by *python*'s own bootstrap, the one the actors import, into the
    auth root of ``USER_DATA_DIR``; refused unless that bootstrap then reads
    the install as ready, links and all.
    """
    result = subprocess.run(
        [python, "-I", "-c", _RECORD_INSTALL],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=env,
    )
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or not lines or lines[-1] != "True":
        raise RuntimeError(
            f"the install at {env.get('PLAYWRIGHT_BROWSERS_PATH')} does not read as "
            f"ready: {result.stdout[-500:]} {result.stderr[-1500:]}"
        )


def private_install(
    python: str,
    locations: Sequence[Path],
    env: dict[str, str],
    stall: StallHost,
    *,
    parent: Path | None = None,
) -> PrivateCache:
    """Point *env* at a row-private cache of *locations*, recorded as installed.

    The row's first read has to find the browser ready there: staging recorded
    the runtime's real cache, and a record for any other path reads as "setup
    in progress" (``record_install``). *env* is updated in place with the cache
    and the stall host; *parent* defaults to a fresh temporary directory,
    resolved, since Windows hands out its 8.3 spelling.
    """
    import tempfile

    made_parent = parent is None
    parent = parent or Path(tempfile.mkdtemp(prefix="h-r11-cache-")).resolve()
    cache: PrivateCache | None = None
    try:
        cache = PrivateCache.build(parent / "browsers", locations)
        env.update(
            {
                "PLAYWRIGHT_BROWSERS_PATH": str(cache.directory),
                **stall_environment(stall),
            }
        )
        record_install(python, env)
        return cache
    except BaseException:
        if cache is not None:
            cache.dismantle()
        if made_parent:
            with contextlib.suppress(OSError):
                parent.rmdir()
        raise


def _link(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(
        getattr(os.path, "isjunction", lambda _p: False)(path)
    )


def _unlink(link: Path) -> None:
    """Remove a link and never what it points at."""
    if not _is_link(link):
        raise RuntimeError(f"{link} is not a link; refusing to remove it")
    if sys.platform == "win32":
        os.rmdir(link)
    else:
        link.unlink()


@dataclass
class PrivateCache:
    """A row-private ``PLAYWRIGHT_BROWSERS_PATH`` of links to installed dirs.

    Links only what the runtime's install names (``install_locations``), so
    patchright finds nothing else here to collect as unused. ``hold_back``
    removes one link, which patchright then reads as a missing dependency;
    ``restore`` puts it back and removes whatever real directory a download
    left in its place. Only links and that row-private download are ever
    removed.
    """

    directory: Path
    sources: list[Path]
    held: Path | None = None
    removed: list[str] = field(default_factory=list)

    @classmethod
    def build(cls, directory: Path, sources: Sequence[Path]) -> PrivateCache:
        directory.mkdir(parents=True, exist_ok=False)
        cache = cls(directory, list(sources))
        try:
            for source in sources:
                if not source.is_dir():
                    raise RuntimeError(f"{source} is not installed")
                _link(source, directory / source.name)
        except BaseException:
            cache.dismantle()
            raise
        return cache

    def hold_back(self) -> Path:
        """Remove the link patchright installs last: winldd, else ffmpeg."""
        names = [source.name for source in self.sources]
        chosen = next(
            (
                n
                for prefix in ("winldd-", "ffmpeg-")
                for n in names
                if n.startswith(prefix)
            ),
            None,
        )
        if chosen is None:
            raise RuntimeError(f"no dependency to hold back among {names}")
        _unlink(self.directory / chosen)
        self.held = self.directory / chosen
        return self.held

    def restore(self) -> None:
        held = self.held
        if held is None:
            return
        source = next(s for s in self.sources if s.name == held.name)
        if held.exists() and not _is_link(held):
            # A download patchright started in this row-private directory.
            import shutil

            shutil.rmtree(held)
            self.removed.append(str(held))
        if not held.exists():
            _link(source, held)
        self.held = None

    def restore_installed(self, python: str, env: dict[str, str]) -> None:
        """Restore the dependency and its matching install record before reuse."""
        self.restore()
        record_install(python, env)

    def dismantle(self) -> None:
        """Remove every link first, then what patchright wrote here itself
        (``.links``, ``__dirlock``), then the directory; never a link's target.
        """
        import shutil

        self.restore()
        for entry in self.directory.iterdir():
            if _is_link(entry):
                _unlink(entry)
        for entry in self.directory.iterdir():
            if entry.is_dir() and not _is_link(entry):
                shutil.rmtree(entry)
            else:
                entry.unlink()
        with contextlib.suppress(OSError):
            os.rmdir(self.directory)


class StallHost:
    """A loopback host that accepts every connection and never answers.

    ``PLAYWRIGHT_DOWNLOAD_HOST`` points here, so an install that needs a
    download stays running for as long as the host does. The connections are
    held, not closed, so the download fails neither fast nor at all until
    ``stop``; the row stops it only once every installer it saw has ended.
    """

    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(16)
        self._held: list[socket.socket] = []
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.getsockname()[1]}"

    def _accept(self) -> None:
        self._server.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._held.append(connection)
            self.connections += 1

    def start(self) -> StallHost:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        for connection in self._held:
            with contextlib.suppress(OSError):
                connection.close()
        with contextlib.suppress(OSError):
            self._server.close()


def stall_environment(host: StallHost) -> dict[str, str]:
    """What sends the installer's downloads to the stall host, and keeps it waiting."""
    return {
        "PLAYWRIGHT_DOWNLOAD_HOST": host.url,
        # Playwright's per-download idle bound, 30 s by default; far past the row.
        "PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT": str(3_600_000),
    }


# --- Installer fate --------------------------------------------------------------

#: FILETIME counts 100 ns ticks from 1601-01-01; this many of them to 1970.
_FILETIME_UNIX_EPOCH = 116_444_736_000_000_000


def filetime_to_unix(ticks: int) -> float:
    """A Windows FILETIME as seconds since the Unix epoch, ``time.time()``'s clock."""
    return (ticks - _FILETIME_UNIX_EPOCH) / 10_000_000


def _open_for_exit_time(pid: int) -> Any | None:
    """A handle that keeps *pid*'s exit code and times readable once it ended."""
    if sys.platform != "win32":
        return None
    import _winapi

    query_limited, synchronize = 0x1000, 0x00100000
    try:
        return _winapi.OpenProcess(query_limited | synchronize, False, pid)
    except OSError:
        return None


def _process_time(handle: Any, index: int) -> float | None:
    """``GetProcessTimes``' creation (0) or exit (1) time for *handle*, or None."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.GetProcessTimes(
        wintypes.HANDLE(int(handle)), *(ctypes.byref(t) for t in times)
    ):
        return None
    chosen = times[index]
    return filetime_to_unix((chosen.dwHighDateTime << 32) | chosen.dwLowDateTime)


class NativeProcess:
    """The Win32 calls a fate is read through, all on one handle.

    Windows only; the tests stand doubles in for it elsewhere.
    """

    def open(self, pid: int) -> Any | None:
        return _open_for_exit_time(pid)

    def created(self, handle: Any) -> float | None:
        return _process_time(handle, 0)

    def wait(self, handle: Any) -> None:
        if sys.platform != "win32":
            raise OSError("no Win32 wait off Windows")
        import _winapi

        answer = _winapi.WaitForSingleObject(handle, _winapi.INFINITE)
        if answer != _winapi.WAIT_OBJECT_0:
            raise OSError(f"WaitForSingleObject answered {answer}")

    def exit_code(self, handle: Any) -> int:
        if sys.platform != "win32":
            raise OSError("no Win32 exit code off Windows")
        import _winapi

        return _winapi.GetExitCodeProcess(handle)

    def exited(self, handle: Any) -> float | None:
        # Read only after the wait returned: for a process still running the
        # exit time GetProcessTimes writes is undefined.
        return _process_time(handle, 1)

    def close(self, handle: Any) -> None:
        if sys.platform != "win32":
            return
        import _winapi

        _winapi.CloseHandle(handle)


#: How far apart two readings of one creation time may be: both are the
#: kernel's FILETIME, read through psutil and through ``GetProcessTimes``.
_CREATED_TOLERANCE_SECONDS = 0.01


@dataclass
class Fate:
    pid: int
    start: float
    #: When the harness's waiter saw the exit: late by however long it took.
    exited_at: float | None = None
    exit_code: int | None = None
    #: When the kernel recorded the exit, on the same clock as ``time.time()``.
    kernel_exit: float | None = None
    #: Why this fate cannot be known: no handle to this lifetime, a failed
    #: wait, no exit time. Never read as an exit, nor as a process still alive.
    problem: str | None = None

    @property
    def ended(self) -> float | None:
        return self.kernel_exit

    @property
    def settled(self) -> bool:
        """An observed exit: a code and the kernel's exit time, from one handle."""
        return (
            self.problem is None
            and self.exit_code is not None
            and self.kernel_exit is not None
        )

    def is_lifetime(self, pid: Any, created: Any) -> bool:
        return (
            pid == self.pid
            and isinstance(created, (int, float))
            and abs(float(created) - self.start) <= _CREATED_TOLERANCE_SECONDS
        )

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_identity": self.start,
            "exited_at": self.exited_at,
            "kernel_exit": self.kernel_exit,
            "exit_code": self.exit_code,
            "problem": self.problem,
        }


class Fates:
    """Each installer process the row saw, watched through one handle.

    The handle is opened when the row identifies the process and is checked to
    name the lifetime the watcher recorded (its creation time), then waited on,
    and the exit code and the kernel's exit time are read from it only once
    that wait returned. Anything short of that leaves the fate's ``problem``
    set: unknown, never an exit and never a survivor.
    """

    def __init__(self, native: Any | None = None) -> None:
        self.native = native if native is not None else NativeProcess()
        self.fates: dict[tuple[int, float], Fate] = {}
        self._threads: list[threading.Thread] = []

    def watch(self, pid: int, start: float) -> None:
        """Watch the lifetime (*pid*, *start*).

        One that cannot be watched stays, as an unknown fate: never dropped,
        so the installer inventory still has to account for it
        (``installer_inventory`` in the harness).
        """
        key = (pid, start)
        if key in self.fates:
            return
        fate = Fate(pid, start)
        native = self.native
        try:
            handle = native.open(pid)
        except Exception as exc:  # noqa: BLE001 - recorded as unknown
            handle, fate.problem = None, f"its handle could not be opened: {exc!r}"
        if handle is None:
            fate.problem = fate.problem or "its handle could not be opened"
        else:
            try:
                created = native.created(handle)
            except Exception:  # noqa: BLE001 - recorded as unknown
                created = None
            if not fate.is_lifetime(pid, created):
                native.close(handle)
                fate.problem = (
                    f"the process the handle names was created at {created}, not "
                    f"the recorded {start}"
                )
        self.fates[key] = fate
        if fate.problem is not None:
            return

        def wait() -> None:
            try:
                native.wait(handle)
                code = native.exit_code(handle)
                ended = native.exited(handle)
            except Exception as exc:  # noqa: BLE001 - recorded as unknown
                fate.problem = f"its wait failed: {exc!r}"
            else:
                if ended is None:
                    fate.problem = "the kernel gave no exit time"
                else:
                    fate.exit_code, fate.kernel_exit = code, ended
                    fate.exited_at = time.time()
            finally:
                with contextlib.suppress(Exception):
                    native.close(handle)

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self._threads.append(thread)

    def alive(self) -> list[Fate]:
        """Watched, and no exit observed yet: still running as far as is known."""
        return [
            fate
            for fate in self.fates.values()
            if fate.problem is None and not fate.settled
        ]

    def unsettled(self) -> list[Fate]:
        """Every fate that is not an observed exit: alive or unknown."""
        return [fate for fate in self.fates.values() if not fate.settled]

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        for thread in self._threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))


#: The routine drain, whose termination of a member it could not place is
#: H-R11's '!'. The baseline's hard-exit drain ``_drain_adopted_windows_job``
#: terminates members too, after the close, and is another act.
ROUTINE_DRAIN = "_drain_adopted_windows_job_members"


@dataclass
class DrainReading:
    """What the routine drain did to the members it asked about, or why that
    is unknown."""

    #: Confirmed: installer lifetimes it terminated successfully, each after
    #: its planted query on that lifetime, each then seen to end with code 1.
    terminated: list[Fate] = field(default_factory=list)
    #: Terminations of an installer it attempted and that failed: a forbidden
    #: act, never a confirmed one.
    attempted: list[dict[str, Any]] = field(default_factory=list)
    #: Terminations, of any outcome, of a known row process that is no
    #: installer (the owner's gate, its launcher, a console host).
    others: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def value(self) -> bool | None:
        """True for '!', False for '=', None for anything else.

        '!' is only a confirmed termination: the positive control K2 is
        calibrated by. '=' is only no act at all, on complete evidence. An
        attempt or a known row process's termination without a confirmed one
        is a forbidden act that proves no termination, and incomplete
        evidence proves nothing: both are None, which neither calibrates K2
        nor passes K3.
        """
        if self.unknown:
            return None
        if self.terminated:
            return True
        if self.attempted or self.others:
            return None
        return False

    def acts(self) -> list[str]:
        """What the drain did short of a confirmed termination, for a report."""
        return [
            *(f"attempted {line.get('member')}" for line in self.attempted),
            *(f"terminated row process {line.get('member')}" for line in self.others),
        ]


def drain_reading(
    fates: Iterable[Fate],
    queried: Iterable[dict[str, Any]],
    terminated: Iterable[dict[str, Any]],
    *,
    known_other: Callable[[Any, Any], bool] = lambda member, created: False,
    health: Iterable[str] = (),
) -> DrainReading:
    """Whether the routine drain terminated, or tried to, a member it failed
    to place.

    From the shim's records of the closing owner alone, never from timing: a
    ``TerminateProcess`` call the routine drain made (``ROUTINE_DRAIN``)
    after the planted query on that same lifetime. Shared setup shutdown ends
    the installer through ``TerminateJobObject`` with the same code 1, and no
    receipt time or exit time tells the two apart; only the caller does.
    Three outcomes stay apart (``DrainReading.value``): a confirmed
    termination (the call succeeded, and that same lifetime then ended with
    code 1); a failed call, which is an attempt, a forbidden act but no
    termination; and a call whose end was never recorded, which may not have
    run at all and is unknown.

    Each member the drain asked about is placed by its lifetime (pid and
    creation time): a watched installer, a row process *known_other* shows is
    no installer nor any installer's descendant, or else unknown. Unknown,
    and so no reading at all, whenever the evidence is not complete: the
    records' own *health* problems (``shim_log``), a relevant fate that is
    not an observed exit, an unplaced queried or terminated lifetime, a
    termination with no planted query before it or no recorded end, or a
    successful one whose installer did not then end with code 1.
    """
    fates = list(fates)
    reading = DrainReading(unknown=list(health))

    def watched(line: dict[str, Any]) -> Fate | None:
        for fate in fates:
            if fate.is_lifetime(line.get("member"), line.get("created")):
                return fate
        return None

    def placed(line: dict[str, Any]) -> tuple[str, Any] | None:
        fate = watched(line)
        if fate is not None:
            return ("installer", fate)
        if known_other(line.get("member"), line.get("created")):
            return ("other", (line.get("member"), line.get("created")))
        return None

    relevant: set[tuple[int, float]] = set()
    queries: dict[Any, float] = {}
    for line in queried:
        where = placed(line)
        if where is None:
            reading.unknown.append(
                f"the drain asked about {line.get('member')} created "
                f"{line.get('created')}, a lifetime the row neither watched nor "
                f"knows as a process that is no installer"
            )
            continue
        key = (where[1].pid, where[1].start) if where[0] == "installer" else where[1]
        if where[0] == "installer":
            relevant.add(key)
        t = float(line.get("t", 0.0))
        queries[key] = min(queries.get(key, t), t)
    for line in terminated:
        if line.get("caller") != ROUTINE_DRAIN:
            continue
        where = placed(line)
        if where is None:
            reading.unknown.append(
                f"the routine drain terminated {line.get('member')} created "
                f"{line.get('created')}, a lifetime the row cannot place"
            )
            continue
        kind, found = where
        key = (found.pid, found.start) if kind == "installer" else found
        asked = queries.get(key)
        began = float(line.get("began") or 0.0)
        if asked is None or asked > began:
            reading.unknown.append(
                f"the routine drain terminated {line.get('member')} without the "
                f"planted query before it"
            )
            continue
        if kind == "installer":
            relevant.add(key)
        if line.get("succeeded") is None:
            # It began and no end was recorded: it may not have run at all,
            # and the records are incomplete whichever member it named.
            reading.unknown.append(
                f"the routine drain's termination of {line.get('member')} began "
                f"and no end was recorded"
            )
            continue
        if kind == "other":
            reading.others.append(line)
            continue
        if line.get("succeeded") is not True:
            reading.attempted.append(line)
            continue
        if not found.settled:
            continue  # unknown below, as a relevant fate that did not settle
        if (
            found.exit_code != 1
            or found.kernel_exit is None
            or found.kernel_exit < began
        ):
            reading.unknown.append(
                f"the routine drain terminated installer {found.pid} at {began}, "
                f"but it ended with {found.exit_code} at {found.kernel_exit}"
            )
            continue
        reading.terminated.append(found)
    for fate in fates:
        # A fate matters here only for a member the drain touched; every
        # installer's end is the inventory's to account for.
        if (fate.pid, fate.start) in relevant and not fate.settled:
            reading.unknown.append(
                f"installer {fate.pid}'s fate is unknown: "
                f"{fate.problem or 'no exit was observed'}"
            )
    return reading
