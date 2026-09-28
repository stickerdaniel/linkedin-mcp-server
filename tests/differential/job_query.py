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
beside the shim (the calling pid, the member asked about, the Job handle), so
a row can say whether its query was reached. Nothing else in any process is
changed. Planting it inside an actor is Daniel's amendment to the plan
(FABLE_PLAN_V7, 2026-09-27): the baseline cannot gain a production seam, and
closing the owner's handle from outside would alter its handle table and keep
the installer's Job alive.

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
from collections.abc import Iterable, Sequence
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
to the real API unchanged.
"""

import json
import os
import sys
import time

_RECORD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "h-r11-reached.jsonl")


def install(win32job, error, process_id, record=_RECORD):
    """Wrap win32job.IsProcessInJob; the doubles in the tests call this too."""
    real = win32job.IsProcessInJob

    def IsProcessInJob(process, job):
        caller = sys._getframe(1)
        if (
            caller.f_code.co_name == "_in_another_owned_job"
            and caller.f_globals.get("__name__") == "linkedin_mcp_server.process_tree"
        ):
            try:
                member = process_id(process)
            except Exception:
                member = None
            try:
                handle = int(job)
            except Exception:
                handle = None
            try:
                with open(record, "a", encoding="utf-8") as stream:
                    line = {"pid": os.getpid(), "t": time.time(), "member": member,
                            "job": handle}
                    stream.write(json.dumps(line) + "\\n")
            except OSError:
                pass
            raise error(5, "IsProcessInJob", "planted by the H-R11 shim")
        return real(process, job)

    win32job.IsProcessInJob = IsProcessInJob


if sys.platform == "win32":
    try:
        import pywintypes
        import win32job
        import win32process

        install(win32job, pywintypes.error, win32process.GetProcessId)
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
    """The shim's module namespace, executed off Windows so ``install`` is
    callable with doubles; on Windows it would also have installed itself."""
    namespace: dict[str, Any] = {"__name__": "sitecustomize", "__file__": "shim"}
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
        [python, "-c", program],
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


def code_difference(source: dict[str, Any], shimmed: dict[str, Any]) -> list[str]:
    """What the shim venv imports differently from its source, besides the shim."""
    problems = []
    for name in ("module", "direct_url", "version"):
        if source.get(name) != shimmed.get(name):
            problems.append(
                f"{name}: the source venv has {source.get(name)!r}, the shim venv "
                f"{shimmed.get(name)!r}"
            )
    if not shimmed.get("sitecustomize"):
        problems.append("the shim venv did not run its sitecustomize")
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
    Path(site_packages, "sitecustomize.py").write_text(SHIM_SOURCE, encoding="utf-8")
    code = _ask(python, _ASK_CODE)
    problems = code_difference(source_code, code)
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


def reached(path: Path, pid: int | None = None) -> list[dict[str, Any]]:
    """The planted failures recorded at *path*, for *pid* when it is given."""
    if not path.is_file():
        return []
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        with contextlib.suppress(ValueError):
            entry = json.loads(line)
            if pid is None or entry.get("pid") == pid:
                lines.append(entry)
    return lines


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
        for source in sources:
            if not source.is_dir():
                raise RuntimeError(f"{source} is not installed")
            _link(source, directory / source.name)
        return cls(directory, list(sources))

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


@dataclass
class Fate:
    pid: int
    start: float
    exited_at: float | None = None
    exit_code: int | None = None

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "start_identity": self.start,
            "exited_at": self.exited_at,
            "exit_code": self.exit_code,
        }


class Fates:
    """Each installer process the row saw, watched from a handle held early.

    On Windows a handle keeps the exit code readable after the process ends,
    which is what tells ``TerminateProcess(handle, 1)`` from any other end.
    """

    def __init__(self) -> None:
        self.fates: dict[tuple[int, float], Fate] = {}
        self._threads: list[threading.Thread] = []

    def watch(self, process: Any, start: float) -> None:
        key = (process.pid, start)
        if key in self.fates:
            return
        fate = self.fates[key] = Fate(process.pid, start)

        def wait() -> None:
            try:
                code = process.wait()
            except Exception:  # noqa: BLE001 - recorded as unknown
                code = None
            fate.exit_code = code
            fate.exited_at = time.time()

        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        self._threads.append(thread)

    def alive(self) -> list[Fate]:
        return [fate for fate in self.fates.values() if fate.exited_at is None]

    def settle(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        for thread in self._threads:
            thread.join(timeout=max(deadline - time.monotonic(), 0.0))


def terminated_in(
    fates: Iterable[Fate], window: tuple[float, float] | None, *, slack: float = 1.0
) -> list[Fate]:
    """The members ended with exit code 1 inside the close's window.

    That is ``TerminateProcess(handle, 1)`` during the routine drain, which is
    inside ``close_session``; *slack* covers the waiter noticing the exit.
    """
    if window is None:
        return []
    began, ended = window
    return [
        fate
        for fate in fates
        if fate.exit_code == 1
        and fate.exited_at is not None
        and began <= fate.exited_at <= ended + slack
    ]
