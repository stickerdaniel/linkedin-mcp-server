"""The frozen baseline: a pinned revision in its own checkout, venv and browser.

K1 frozen and K2 run the server as it was before the default-on changes, at
``BASELINE_SHA``: the last ``main`` commit before P1 (#1122), P3a (#1125,
custom browsers stay Direct) and the patchright 1.63.0 lock (#1123). It has its
own ``uv.lock``, so it gets its own ``uv sync`` venv, runtime dependencies only,
and its own ``patchright install`` into a browser cache of its own. Nothing of
it is imported into the harness: every baseline actor is started from the
baseline venv's interpreter, and the staging and the questions the harness has
to ask of the baseline (its identity, its bundled browser) are asked of that
interpreter in a subprocess.

**Refused unless it is exactly the pin.** The checkout's ``HEAD`` must be the
pinned SHA and ``git status --porcelain`` must be empty, checked before and
after the venv is built; a checkout that is anything else would be measured
under the baseline's name.

**Which interpreter ran is read from the watcher.** The frontend is started as
``<baseline python> -m linkedin_mcp_server``, and production starts the owner
from ``sys.executable``, which is that same path. The watcher records each
actor's command line, so a row can show from its own records that the frontend
and the owner ran the baseline's interpreter and that none ran the candidate's.
The executable psutil reports is no help here: a venv's interpreter is a link
to the same base Python for both checkouts. On Windows the venv's
``python.exe`` is a launcher that starts the base interpreter as its child, so
the rule asks for at least one frontend (and owner) record naming the baseline
interpreter and for none naming the candidate's, not for every record to. On
macOS with a framework build (the runner's python.org Python) neither the
executable nor ``argv[0]`` names the venv: the venv's ``bin/python`` stub
re-executes ``Python.app``, whose binary becomes both. It leaves the venv path
in ``__PYVENV_LAUNCHER__``, which the watcher records for a server's
processes as ``launcher`` (``watcher.read_launcher``), so the rule reads that
as well as ``argv[0]``.

Run as a script, it prepares the runtime: ``python baseline.py DIRECTORY``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The pinned baseline: "test(daemon): Witness daemon-only regressions (#1115)".
BASELINE_SHA = "0253421539fffd4c9b207ca62b6efb41a8905ed3"

#: Where CI prepared the baseline before the rows run; a temporary one if unset.
BASELINE_DIR_ENV = "LINKEDIN_MCP_DIFFERENTIAL_BASELINE"

PACKAGE = "mcp-server-linkedin"
REPO_ROOT = Path(__file__).resolve().parents[2]
STAGE_SCRIPT = Path(__file__).with_name("baseline_stage.py")

#: Environment that would point ``uv`` at another project's venv.
_FOREIGN_VENV = ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT")


class BaselineRefused(RuntimeError):
    """The baseline checkout or its runtime is not exactly the pinned revision."""


@dataclass(frozen=True)
class Runtime:
    """The code a row's actors run: interpreter, checkout and browser cache."""

    python: str
    checkout: Path
    browsers: Path
    #: The pinned revision of a frozen baseline; None for this checkout.
    pinned: str | None = None

    @property
    def frozen(self) -> bool:
        return self.pinned is not None

    @property
    def short(self) -> str:
        return (self.pinned or "")[:7]

    def command(self) -> list[str]:
        return [self.python, "-m", "linkedin_mcp_server"]


def _run(
    command: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def git(repo: Path, *args: str) -> str | None:
    """Git's output, or None when it failed."""
    try:
        result = _run(["git", *args], cwd=repo, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def checkout_state(checkout: Path) -> dict[str, Any]:
    """HEAD, cleanliness and lock of a checkout, as a row records them."""
    head = git(checkout, "rev-parse", "HEAD")
    # ``--no-optional-locks``: reading the state must not write the index.
    porcelain = git(checkout, "--no-optional-locks", "status", "--porcelain")
    lock = checkout / "uv.lock"
    return {
        "checkout": str(checkout),
        "head": head.strip() if head else None,
        "porcelain_empty": porcelain == "" if porcelain is not None else None,
        "dirty_paths": (porcelain or "").splitlines()[:50],
        "uv_lock_sha256": (
            hashlib.sha256(lock.read_bytes()).hexdigest() if lock.is_file() else None
        ),
    }


def checkout_refusal(state: dict[str, Any], pinned: str) -> str | None:
    """Why a checkout is not exactly *pinned*, or None."""
    if state.get("head") != pinned:
        return (
            f"the baseline checkout {state.get('checkout')} is at "
            f"{state.get('head')}, not the pinned {pinned}"
        )
    if state.get("porcelain_empty") is not True:
        return (
            f"the baseline checkout {state.get('checkout')} is not clean: "
            f"{state.get('dirty_paths')}"
        )
    return None


def verify_checkout(checkout: Path, pinned: str = BASELINE_SHA) -> dict[str, Any]:
    state = checkout_state(checkout)
    refusal = checkout_refusal(state, pinned)
    if refusal is not None:
        raise BaselineRefused(refusal)
    return state


def baseline_file(
    relative: str, *, pinned: str = BASELINE_SHA, repo: Path = REPO_ROOT
) -> str:
    """One file of the pinned revision, fetched first when a shallow clone lacks it.

    For a model control that runs the baseline's own function against doubles,
    in a job that never prepares the whole baseline.
    """
    if git(repo, "cat-file", "-e", f"{pinned}^{{commit}}") is None:
        fetched = _run(
            ["git", "fetch", "--no-tags", "--depth=1", "origin", pinned],
            cwd=repo,
            timeout=300,
        )
        _checked(fetched, f"fetching {pinned}")
    shown = git(repo, "show", f"{pinned}:{relative}")
    if shown is None:
        raise BaselineRefused(f"{relative} is not in {pinned}")
    return shown


def venv_python(checkout: Path) -> Path:
    if sys.platform == "win32":
        return checkout / ".venv" / "Scripts" / "python.exe"
    return checkout / ".venv" / "bin" / "python"


def _checked(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode != 0:
        raise BaselineRefused(
            f"{what} failed with status {result.returncode}: "
            f"{(result.stderr or result.stdout)[-2000:]}"
        )


def prepare_baseline(
    directory: Path,
    *,
    pinned: str = BASELINE_SHA,
    repo: Path = REPO_ROOT,
    timings: dict[str, float] | None = None,
) -> Runtime:
    """Check out, sync and equip the baseline under *directory*; idempotent.

    ``directory/checkout`` is a detached worktree of this repository at the
    pin, fetched first when a shallow clone lacks it; ``directory/ms-playwright``
    is its browser cache. A second call adds no worktree and downloads nothing:
    it re-runs the frozen ``uv sync`` and the browser install, which find
    everything in place and so verify it, then checks the checkout again.
    """
    timings = {} if timings is None else timings
    directory.mkdir(parents=True, exist_ok=True)
    checkout = directory / "checkout"
    browsers = directory / "ms-playwright"

    began = time.monotonic()
    if not checkout.exists():
        if git(repo, "cat-file", "-e", f"{pinned}^{{commit}}") is None:
            fetched = _run(
                ["git", "fetch", "--no-tags", "--depth=1", "origin", pinned],
                cwd=repo,
                timeout=300,
            )
            _checked(fetched, f"fetching {pinned}")
        added = _run(
            ["git", "worktree", "add", "--detach", str(checkout), pinned],
            cwd=repo,
            timeout=300,
        )
        _checked(added, f"adding the baseline worktree at {checkout}")
    verify_checkout(checkout, pinned)
    timings["checkout"] = round(time.monotonic() - began, 1)

    env = {k: v for k, v in os.environ.items() if k not in _FOREIGN_VENV}
    began = time.monotonic()
    # Runtime dependencies only, exactly as the baseline's lock pins them.
    synced = _run(
        ["uv", "sync", "--frozen", "--no-dev"], cwd=checkout, env=env, timeout=900
    )
    _checked(synced, "uv sync in the baseline checkout")
    timings["uv_sync"] = round(time.monotonic() - began, 1)
    python = venv_python(checkout)
    if not python.exists():
        raise BaselineRefused(f"uv sync left no interpreter at {python}")

    began = time.monotonic()
    browsers.mkdir(parents=True, exist_ok=True)
    installed = _run(
        [str(python), "-m", "patchright", "install", "chromium", "--no-shell"],
        env={**env, "PLAYWRIGHT_BROWSERS_PATH": str(browsers)},
        timeout=900,
    )
    _checked(installed, "the baseline's patchright install")
    timings["browser"] = round(time.monotonic() - began, 1)
    # After the venv and the browser: neither may leave the checkout dirty.
    verify_checkout(checkout, pinned)
    return Runtime(str(python), checkout, browsers, pinned)


def remove_baseline(directory: Path, *, repo: Path = REPO_ROOT) -> None:
    """Remove the worktree ``prepare_baseline`` added, for local runs."""
    checkout = directory / "checkout"
    if checkout.exists():
        _run(["git", "worktree", "remove", "--force", str(checkout)], cwd=repo)


# --- Asking the baseline, in its own interpreter --------------------------------

_IDENTITY_PROGRAM = """
import json, sys, sysconfig
from importlib.metadata import distributions
site = sysconfig.get_paths()["purelib"]
installed = list(distributions(name=%(package)r, path=[site]))
raw = installed[0].read_text("direct_url.json") if len(installed) == 1 else None
print(json.dumps({
    "sys_executable": sys.executable,
    "site_packages": site,
    "installed_distributions": len(installed),
    "direct_url": json.loads(raw) if raw else None,
}))
""" % {"package": PACKAGE}

_EXECUTABLE_PROGRAM = """
from patchright.sync_api import sync_playwright
with sync_playwright() as p:
    print(p.chromium.executable_path)
"""


def _ask(runtime: Runtime, program: str, *, env: dict[str, str] | None = None) -> str:
    # ``-I``: nothing from the harness's environment or working directory can
    # put other code on the baseline interpreter's path.
    result = _run([runtime.python, "-I", "-c", program], env=env, timeout=120)
    _checked(result, "asking the baseline interpreter")
    return result.stdout.strip()


def frozen_identity(runtime: Runtime) -> dict[str, Any]:
    """What a baseline row's actors import and run, from the baseline itself."""
    installed = json.loads(_ask(runtime, _IDENTITY_PROGRAM))
    return {
        "reference": f"frozen baseline {runtime.pinned}",
        "pinned": runtime.pinned,
        "interpreter": runtime.python,
        **installed,
        **checkout_state(runtime.checkout),
    }


def bundled_executable(runtime: Runtime) -> str:
    """The Chromium the baseline's patchright launches from its own cache."""
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(runtime.browsers)}
    return _ask(runtime, _EXECUTABLE_PROGRAM, env=env).splitlines()[-1]


def stage_frozen_session(
    runtime: Runtime, profile: Path, env: dict[str, str], *, timeout: float = 600
) -> None:
    """Validate and commit the staged cookie file with the baseline's own code.

    The cookie file is already written; the baseline's import validation then
    launches the baseline's browser on the profile, so the profile starts from
    the browser build that will run it, never from a newer one.
    """
    result = _run(
        [runtime.python, str(STAGE_SCRIPT), str(profile)],
        cwd=runtime.checkout,
        env=env,
        timeout=timeout,
    )
    _checked(result, "staging the session with the baseline")


# --- Which interpreter the row's actors ran ------------------------------------


def _spelled(path: str) -> str:
    """One spelling of an interpreter path, without following the file itself.

    The directory is resolved, so ``/var`` and ``/private/var`` on macOS agree,
    and the name is kept: a venv's ``bin/python`` is a link to the base
    interpreter, and following it would lose which venv it belongs to.
    """
    absolute = os.path.abspath(path)
    directory = os.path.realpath(os.path.dirname(absolute))
    return os.path.normcase(os.path.join(directory, os.path.basename(absolute)))


def _interpreters(record: dict[str, Any]) -> list[str]:
    """Every interpreter path a watcher record names: argv[0], and the launcher."""
    named = []
    cmdline = record.get("cmdline")
    if isinstance(cmdline, list) and cmdline:
        named.append(str(cmdline[0]))
    if isinstance(record.get("launcher"), str) and record["launcher"]:
        named.append(record["launcher"])
    return [_spelled(path) for path in named]


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def interpreter_failures(
    records: Iterable[dict[str, Any]],
    runtime: Runtime,
    *,
    candidate_prefix: str,
    owner_expected: bool,
) -> list[str]:
    """Why the watcher's records do not show the row ran *runtime*'s code.

    A frontend or owner the row started that names an interpreter in the
    candidate's environment, by ``argv[0]`` or by launcher, is candidate code
    in a baseline row, whatever else it names. At least one frontend, and one
    owner when an owner is expected, must name the baseline interpreter.
    """
    baseline = _spelled(runtime.python)
    candidate = os.path.normcase(os.path.realpath(candidate_prefix))
    seen: dict[str, int] = {"frontend": 0, "owner": 0}
    failures = []
    for record in records:
        if record.get("kind") not in ("process.start", "process.update"):
            continue
        if record.get("in_row") is not True:
            continue
        actor = str(record.get("actor"))
        if actor not in seen:
            continue
        named = _interpreters(record)
        foreign = [path for path in named if _under(path, candidate)]
        if foreign:
            failures.append(
                f"the {actor} (pid {record.get('pid')}) ran the candidate's "
                f"interpreter {foreign[0]} in a baseline row"
            )
        elif baseline in named:
            seen[actor] += 1
    if not seen["frontend"]:
        failures.append(
            f"no frontend the watcher saw ran the baseline interpreter {baseline}"
        )
    if owner_expected and not seen["owner"]:
        failures.append(
            f"no owner the watcher saw ran the baseline interpreter {baseline}"
        )
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: baseline.py DIRECTORY", file=sys.stderr)
        return 2
    timings: dict[str, float] = {}
    runtime = prepare_baseline(Path(args[0]), timings=timings)
    print(
        json.dumps(
            {
                "pinned": runtime.pinned,
                "python": runtime.python,
                "browsers": str(runtime.browsers),
                "executable": bundled_executable(runtime),
                "seconds": timings,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
