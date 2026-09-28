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
beside the shim: the calling pid and that process's own creation time, the
member asked about and its creation time, the Job handle and the wall-clock
time. Nothing else in any process is changed. Planting it inside an actor is
Daniel's amendment to the plan (FABLE_PLAN_V7, 2026-09-27): the baseline
cannot gain a production seam, and closing the owner's handle from outside
would alter its handle table and keep the installer's Job alive.

**Two logger events.** The same shim is also a declared, test-only observer
of exactly two logger events: ``core.close`` logging that the drain did not
prove the launch gone (its consumption of the drain's False, logged for no
exception), and the owner's ``Standing down: %s``. A filter on each of the
two loggers matches the logger's name and the record's exact message
template, and returns True whatever happens, so no record, handler or call
changes; a record of it carries the process's pid, its own creation time and
a monotonic reading. The daemon log is one file per auth root, shared by
every owner generation, so only this tells which lifetime reached that call
(review e1ey, E1EY-02). It says the call was reached, not that anything was
written to the log.

**Positive evidence only.** A record says that one planted failure, or one of
the two events, was reached by that lifetime, then. It certifies that
invocation and nothing else: not that no other call was made, not what the
caller did next. A record that could not be written is simply absent, and
nothing reads an absence as evidence. So the shim observes no
``TerminateProcess`` call, and nothing here says which native caller ended an
installer: exit code 1 comes from the routine drain and from a Job's rundown
alike (review e1ex, E1EX-02, which retired the observer that tried).

**Claim map.** What the row establishes, and from which evidence, never added
together:

* The routine drain's branch on an unanswered held-Job query: the exact
  baseline and candidate functions run against Win32 doubles
  (``job_query_model``). Source-model evidence of a conditional branch.
* That each native experiment reached that situation and what followed it:
  the owner that closed, a positive fault record about an installer lifetime
  inside the close, that same lifetime reaching core.close's consumption of
  the drain's False and its stand-down, the installer family's settlement,
  and a successor serving afterwards (``harness.NativeContinuation``). Native
  evidence of the continuation, with the termination cause recorded as
  unobserved.
* How the installer family ends after an unconfirmed close: the candidate's
  owner exits without a signal and its kill-on-close Jobs run down, the same
  primitive that ends the Direct reference's family at host quit. A shared
  path, disposed of by that source reduction and the settlement observed
  here, not by a native census of recipients. Whole-system O2 stays
  unobserved where nothing traced it.

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
from collections.abc import Sequence
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
to the real API unchanged. Each failure it plants is recorded with the
lifetime that made the call. It also observes two logger events, and changes
nothing about them. A record that could not be written is absent, and nothing
reads an absence as evidence.
"""

import json
import os
import sys
import time

_RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "h-r11-reached.jsonl")
_MODULE = "linkedin_mcp_server.process_tree"
#: The two logger events observed, by logger name and exact message template:
#: core.close consuming the drain's False, and the owner's stand-down.
_EVENTS = {
    ("linkedin_mcp_server.core.browser",
     "Browser processes from this launch are still running after close, so the "
     "shutdown stays unconfirmed."): "consumed-false",
    ("linkedin_mcp_server.daemon_owner", "Standing down: %s"): "stand-down",
}


def _write(record, line):
    try:
        with open(record, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line) + "\\n")
    except Exception:
        pass


def _member(identity, handle):
    try:
        member, created = identity(handle)
    except Exception:
        return None, None
    return member, created


def install(win32job, error, identity, record=_RECORD, created=None):
    """Wrap win32job.IsProcessInJob; the doubles in the tests call this too.

    *created* is this process's own creation time: with its pid, the lifetime
    every record it writes comes from.
    """
    real = win32job.IsProcessInJob

    def IsProcessInJob(process, job):
        caller = sys._getframe(1)
        if (
            caller.f_code.co_name == "_in_another_owned_job"
            and caller.f_globals.get("__name__") == _MODULE
        ):
            member, member_created = _member(identity, process)
            try:
                handle = int(job)
            except Exception:
                handle = None
            _write(record, {"kind": "query", "t": time.time(),
                            "monotonic_ns": time.monotonic_ns(), "pid": os.getpid(),
                            "pid_created": created, "member": member,
                            "created": member_created, "job": handle})
            raise error(5, "IsProcessInJob", "planted by the H-R11 shim")
        return real(process, job)

    win32job.IsProcessInJob = IsProcessInJob


def witness(logging, record=_RECORD, created=None):
    """Observe the two logger events in _EVENTS; the doubles call this too.

    A filter on each of the two loggers, matching the record's logger name and
    its exact message template, never the formatted text. It returns True
    whatever happens, so the record, its handlers and the call go on exactly
    as without it; a failure while recording only loses the evidence. A record
    written here says the code reached that logging call in this lifetime,
    not that any handler wrote it anywhere.
    """

    def observed(entry):
        try:
            name = entry.name
            if name == "__main__":
                spec = getattr(sys.modules.get("__main__"), "__spec__", None)
                if getattr(spec, "name", None) == "linkedin_mcp_server.daemon_owner":
                    name = "linkedin_mcp_server.daemon_owner"
            event = _EVENTS.get((name, entry.msg))
            if event is not None:
                args = entry.args if isinstance(entry.args, tuple) else ()
                reason = args[0] if args and isinstance(args[0], str) else None
                _write(record, {"kind": "log", "event": event, "t": time.time(),
                                "monotonic_ns": time.monotonic_ns(),
                                "pid": os.getpid(), "pid_created": created,
                                "reason": reason})
        except Exception:
            pass
        return True

    for name in sorted({logger for logger, _ in _EVENTS} | {"__main__"}):
        logging.getLogger(name).addFilter(observed)


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
    try:
        import win32api

        _created = _times(win32api.GetCurrentProcess())
    except Exception:
        _created = None
    try:
        import pywintypes
        import win32job

        install(win32job, pywintypes.error, _identity, created=_created)
    except Exception:
        pass
    try:
        import logging

        witness(logging, created=_created)
    except Exception:
        pass
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


def _records(path: Path, kind: str) -> list[dict[str, Any]]:
    """Every record of *kind* at *path*, whoever wrote it.

    A line that does not read as a record is skipped: it witnesses nothing,
    and its absence from the result is not read as anything either.
    """
    if not path.is_file():
        return []
    found = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("kind") == kind:
            found.append(entry)
    return found


def reached(path: Path) -> list[dict[str, Any]]:
    """Every planted failure recorded at *path*: query records, and only those."""
    return _records(path, "query")


def logged(path: Path) -> list[dict[str, Any]]:
    """Every observed logger event recorded at *path*, and never a query."""
    return _records(path, "log")


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
