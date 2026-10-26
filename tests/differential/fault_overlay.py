"""R7's fault overlay, the row's scenario, and the judgement of the selected call.

**The overlay.** A venv of its own for each runtime, made with ``venv
--without-pip`` from the runtime's base interpreter, holding exactly two files:
``r7_fault.py``, byte for byte the declared text beside this module, and one
``.pth`` whose two executable lines add the runtime's own ``site-packages``
with ``site.addsitedir`` (so the runtime's editable install is processed as it
is there) and then arm the fault. Row data never goes into that text: the
row's directory reaches the actors as ``R7_FAULT_DIR``. Each overlay is probed
against its source interpreter, in the actors' ``-P`` and ``-I`` modes and
from a neutral directory, and refused unless it imports the same product, from
the same file and install record, with the same dependency, and runs the
declared fault text; under the guardian's ``-I -S`` it must run no fault.

**The scenario** (review e1ez, E1EZ-01). ``BROWSER_IDLE_TIMEOUT=0`` in every
experiment from actor startup: an idle close that began before the harness
armed the fault would otherwise reach the drain after the requested close was
sent, and no entry time can tell the two apart.

**The judgement.** The selected call is established only by the activation,
a complete claim and outcome from the original lifetime, entered after the
requested close was sent, a real True, no invalid record, and exactly one
consumption of the False by that same lifetime after the drain returned,
followed by its keeping the lease. The files alone never establish that the
False was consumed. A lost record is absence of evidence and fails the row;
nothing here claims that the records are complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from differential import r7_fault

FAULT_FILE = Path(r7_fault.__file__)
FAULT_TEXT = FAULT_FILE.read_bytes()
FAULT_SHA256 = hashlib.sha256(FAULT_TEXT).hexdigest()
PTH_FILE = "r7_fault_overlay.pth"
PACKAGE = "mcp-server-linkedin"

#: The one setting the R7 scenario fixes in every experiment (E1EZ-01).
IDLE_TIMEOUT_KEY = "BROWSER_IDLE_TIMEOUT"
SCENARIO = {IDLE_TIMEOUT_KEY: "0"}


def pth_text(source_purelib: str) -> str:
    """The overlay's ``.pth``: the runtime's own site directory, then the fault."""
    return (
        f"import site; site.addsitedir({source_purelib!r})\n"
        "import r7_fault; r7_fault.arm()\n"
    )


def scenario_problems(environment: Mapping[str, str]) -> list[str]:
    """Why an actor environment is not the R7 scenario; empty when it is."""
    raw = environment.get(IDLE_TIMEOUT_KEY)
    try:
        value = float(raw) if raw is not None else None
    except ValueError:
        value = None
    if value != 0:
        return [
            f"{IDLE_TIMEOUT_KEY} is {raw!r}, not 0: an idle close begun before the "
            f"fault was armed could reach the drain after the requested close"
        ]
    return []


def guardian_group_problems(
    experiment: str, group: int | None, *, owner_pgid: int | None = None
) -> list[str]:
    """Why the original guardian's group argument is not its experiment's.

    E1EZ-03: the frozen Direct reference and the candidate OWNER pass zero, as
    a server that leads no group does; the frozen OWNER leads its group and
    passes its own observed PGID. That is K2's recorded behaviour, not a shim
    failure, and it is not the owner's own-group operation either.
    """
    expected = owner_pgid if experiment == "K2" else 0
    if expected is None:
        return [f"{experiment}: the owner's group was not observed"]
    if group != expected:
        return [f"{experiment}: the guardian was given group {group!r}, not {expected}"]
    return []


def pre_probe_problems(
    *, owner_exit: str | None, guardian_exit: str | None, lock: Mapping[str, Any]
) -> list[str]:
    """Why the post-settlement probe may not be requested yet (E1EZ-02).

    The original owner and its guardian positively exited, and the lock free
    by a non-announcing contender. A lock found held is not settled, whoever
    turns out to hold it: an elected successor may exist, but not one already
    using the profile before this checkpoint.
    """
    problems = []
    if owner_exit != "exited":
        problems.append(f"the original owner is {owner_exit!r}, not shown exited")
    if guardian_exit != "exited":
        problems.append(f"the original guardian is {guardian_exit!r}, not shown exited")
    if lock.get("state") != "free":
        problems.append(
            f"the profile lock is {lock.get('state')!r} before the probe: "
            f"{lock.get('reason')}"
        )
    return problems


# --- The overlay and its provenance -------------------------------------------

_ASK_SOURCE = """
import json, sys, sysconfig
print(json.dumps({"base": getattr(sys, "_base_executable", sys.executable),
                  "purelib": sysconfig.get_paths()["purelib"]}))
"""

#: What an interpreter imports, asked of it in an actor's mode.
PROBE = """
import json, sys
from importlib import metadata
report = {
    "executable": sys.executable,
    "prefix": sys.prefix,
    "flags": [sys.flags.isolated, sys.flags.safe_path, sys.flags.no_site],
    "fault": getattr(sys.modules.get("r7_fault"), "__file__", None),
}
try:
    import linkedin_mcp_server
    import linkedin_mcp_server.process_tree as tree
    report["module"] = linkedin_mcp_server.__file__
    report["process_tree"] = tree.__file__
    report["patched"] = bool(
        getattr(tree._drain_marked_posix_groups, "__r7_fault__", False)
    )
    report["alias_globals"] = tree.drain_browser_process_marker.__globals__ is vars(tree)
    installs = list(metadata.distributions(name="mcp-server-linkedin"))
    report["installs"] = len(installs)
    if len(installs) == 1:
        report["version"] = installs[0].version
        report["direct_url"] = json.loads(
            installs[0].read_text("direct_url.json") or "null"
        )
    try:
        patchright = metadata.distribution("patchright")
        report["patchright"] = [patchright.version, str(patchright.locate_file(""))]
    except metadata.PackageNotFoundError:
        report["patchright"] = None
except Exception as exc:
    report["import_error"] = repr(exc)
print(json.dumps(report))
"""

#: The actors' interpreter modes: an owner's ``-P``, an isolated ``-I``.
MODES = {"safe-path": ["-P"], "isolated": ["-I"]}
#: The guardian's: no site processing, so no overlay and no fault.
GUARDIAN_MODE = ["-I", "-S"]
#: What must be the same in the overlay as in its source runtime.
_SAME = ("module", "process_tree", "installs", "version", "direct_url", "patchright")


def run_python(
    python: str,
    flags: Sequence[str],
    program: str,
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float = 120,
) -> dict[str, Any]:
    """Run *program* under *python* with *flags*; its JSON report."""
    result = subprocess.run(
        [python, *flags, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        cwd=cwd,
        env=dict(environment),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{python} {' '.join(flags)} failed ({result.returncode}): "
            f"{result.stderr[-2000:]}"
        )
    return json.loads(result.stdout.strip().splitlines()[-1])


def actor_environment(fault_dir: Path | None) -> dict[str, str]:
    """This process's environment as an actor would get it: no ``PYTHONPATH``,
    and the row's fault directory when one is given."""
    environment = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    environment.pop(r7_fault.FAULT_DIR_ENV, None)
    if fault_dir is not None:
        environment[r7_fault.FAULT_DIR_ENV] = str(fault_dir)
    return environment


def provenance_problems(
    source: Mapping[str, Any], overlay: Mapping[str, Any], *, fault_file: Path
) -> list[str]:
    """Why the overlay does not run its source's code plus the declared fault."""
    problems = []
    for report, name in ((source, "source"), (overlay, "overlay")):
        if report.get("import_error"):
            problems.append(f"the {name} could not import: {report['import_error']}")
        if report.get("installs") != 1:
            problems.append(
                f"the {name} sees {report.get('installs')} installs of {PACKAGE}"
            )
        if report.get("alias_globals") is not True:
            problems.append(f"the {name}'s public drain has other globals")
    for name in _SAME:
        if source.get(name) != overlay.get(name):
            problems.append(
                f"{name}: the source has {source.get(name)!r}, the overlay "
                f"{overlay.get(name)!r}"
            )
    if source.get("fault") is not None or source.get("patched"):
        problems.append("the source runtime runs a fault of its own")
    actual = overlay.get("fault")
    if not actual or Path(actual).resolve() != fault_file.resolve():
        problems.append(f"the overlay's fault is {actual!r}, not {fault_file}")
    return problems


@dataclass(frozen=True)
class Overlay:
    """One runtime's overlay, and what it and its source were shown to run."""

    directory: Path
    python: str
    source_python: str
    source_purelib: str
    purelib: str
    fault_sha256: str
    pth_sha256: str
    reports: Mapping[str, Mapping[str, Any]]

    @property
    def fault_file(self) -> Path:
        return Path(self.purelib) / "r7_fault.py"


def make_overlay(source_python: str, directory: Path) -> Overlay:
    """An overlay of *source_python*'s runtime at *directory*, or a refusal."""
    with tempfile.TemporaryDirectory(prefix="r7-overlay-probe-") as neutral:
        cwd = Path(neutral)
        plain = actor_environment(None)
        source = run_python(
            source_python, ["-I"], _ASK_SOURCE, cwd=cwd, environment=plain
        )
        made = subprocess.run(
            [source["base"], "-m", "venv", "--without-pip", str(directory)],
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )
        if made.returncode != 0:
            raise RuntimeError(f"venv failed: {made.stderr[-2000:]}")
        python = str(
            directory
            / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        )
        purelib = run_python(python, ["-I"], _ASK_SOURCE, cwd=cwd, environment=plain)[
            "purelib"
        ]
        pth = pth_text(source["purelib"])
        Path(purelib, "r7_fault.py").write_bytes(FAULT_TEXT)
        Path(purelib, PTH_FILE).write_text(pth, encoding="utf-8")
        armed_dir = cwd / "fault"
        armed_dir.mkdir()
        reports: dict[str, Mapping[str, Any]] = {}
        problems = []
        if hashlib.sha256(Path(purelib, "r7_fault.py").read_bytes()).hexdigest() != (
            FAULT_SHA256
        ):
            problems.append("the overlay does not hold the declared fault text")
        for mode, flags in MODES.items():
            armed = actor_environment(armed_dir)
            reports[f"source {mode}"] = run_python(
                source_python, flags, PROBE, cwd=cwd, environment=armed
            )
            reports[f"overlay {mode}"] = run_python(
                python, flags, PROBE, cwd=cwd, environment=armed
            )
            problems += [
                f"{mode}: {problem}"
                for problem in provenance_problems(
                    reports[f"source {mode}"],
                    reports[f"overlay {mode}"],
                    fault_file=Path(purelib, "r7_fault.py"),
                )
            ]
            if reports[f"overlay {mode}"].get("patched") is not True:
                problems.append(f"{mode}: the armed overlay left the drain as it was")
        guardian = run_python(
            python,
            GUARDIAN_MODE,
            PROBE,
            cwd=cwd,
            environment=actor_environment(armed_dir),
        )
        reports["overlay guardian"] = guardian
        if guardian.get("fault") is not None or guardian.get("patched"):
            problems.append("the guardian's mode ran the fault")
        if problems:
            raise RuntimeError(
                f"the overlay does not run its source's code: {problems}"
            )
    return Overlay(
        directory=directory,
        python=python,
        source_python=source_python,
        source_purelib=source["purelib"],
        purelib=purelib,
        fault_sha256=FAULT_SHA256,
        pth_sha256=hashlib.sha256(pth.encode()).hexdigest(),
        reports=reports,
    )


# --- The activation ----------------------------------------------------------------


def process_start_ticks(pid: int) -> int | None:
    """When *pid* started, in kernel ticks; None off Linux or when unreadable."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return int(text[text.rfind(")") + 2 :].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def publish_activation(
    directory: Path,
    *,
    row: str,
    experiment: str,
    repetition: int,
    run: str,
    pid: int,
    start_ticks: int | None,
    role: str,
    marker: str,
    source: Mapping[str, Any],
    monotonic_ns=time.monotonic_ns,
) -> dict[str, Any]:
    """Arm the fault for the original lifetime, once and for good.

    The raw marker stays in this process: only its digest is written. The
    record appears whole or not at all, and a second activation in the same
    directory is refused, so nothing rearms it.
    """
    if start_ticks is None:
        raise ValueError(f"pid {pid}'s start is unknown, so no lifetime can be bound")
    record = {
        "row": row,
        "experiment": experiment,
        "repetition": repetition,
        "run": run,
        "nonce": secrets.token_hex(16),
        "pid": pid,
        "start_ticks": start_ticks,
        "role": role,
        "marker_digest": r7_fault.marker_digest(marker),
        "fault_sha256": FAULT_SHA256,
        "source": dict(source),
        "published_ns": monotonic_ns(),
    }
    final = directory / r7_fault.ACTIVATION
    temporary = directory / f"{r7_fault.ACTIVATION}.{record['nonce']}.tmp"
    temporary.write_text(json.dumps(record), encoding="utf-8")
    try:
        os.link(temporary, final)
    finally:
        temporary.unlink()
    return record


# --- The judgement ---------------------------------------------------------------------


def _read(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """A complete JSON record at *path*, or why there is none."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no {path.name}"
    except (OSError, ValueError) as exc:
        return None, f"{path.name} is not a complete record: {exc!r}"
    if not isinstance(record, dict):
        return None, f"{path.name} is not a complete record"
    return record, None


def events(directory: Path) -> list[dict[str, Any]]:
    """Every observed logger event in *directory*; an unreadable line is none."""
    found = []
    path = directory / r7_fault.EVENTS
    if not path.is_file():
        return found
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            found.append(entry)
    return found


def selection_problems(
    directory: Path, *, activation: Mapping[str, Any], sent_ns: int | None
) -> list[str]:
    """Why the selected True-to-False was not established and consumed.

    *sent_ns* is when the harness sent the requested close, on the monotonic
    clock every process on the machine shares: an entry before it belongs to
    another close, however the files look.
    """
    problems = []
    lifetime = [activation.get("pid"), activation.get("start_ticks")]
    claim, why = _read(directory / r7_fault.CLAIM)
    if claim is None:
        problems.append(f"the selected entry was not established: {why}")
    else:
        for name in ("nonce", "pid", "start_ticks", "marker_digest", "role"):
            if claim.get(name) != activation.get(name):
                problems.append(
                    f"the claim's {name} is {claim.get(name)!r}, the activation's "
                    f"{activation.get(name)!r}"
                )
        entered = claim.get("entered_ns")
        if type(entered) is not int or type(sent_ns) is not int or entered <= sent_ns:
            problems.append(
                f"the claimed entry at {entered} is not after the requested close was "
                f"sent at {sent_ns}"
            )
    for invalid in sorted(directory.glob(f"{r7_fault.INVALID_PREFIX}*")):
        record, why = _read(invalid)
        problems.append(f"invalid entry: {record.get('reason') if record else why}")
    outcome, why = _read(directory / r7_fault.OUTCOME)
    returned = None
    if outcome is None:
        problems.append(f"the selected outcome was not published: {why}")
    else:
        if claim is not None and [outcome.get("nonce"), outcome.get("entered_ns")] != [
            claim.get("nonce"),
            claim.get("entered_ns"),
        ]:
            problems.append("the outcome is not the claim's")
        if outcome.get("real") is not True:
            problems.append(
                f"the real drain answered {outcome.get('real')!r}, not True: no "
                f"calibration"
            )
        returned = outcome.get("returned_ns")
        began = outcome.get("entered_ns")
        if type(returned) is not int:
            problems.append("the outcome has no return time")
            returned = None
        elif type(began) is not int or returned < began:
            # One clock and one call: the return can share the entry's tick,
            # never precede it.
            problems.append(
                f"the outcome returned at {returned}, before its entry at {began}"
            )
    published = activation.get("published_ns")
    mine = [
        event
        for event in events(directory)
        if [event.get("pid"), event.get("start_ticks")] == lifetime
        and type(event.get("monotonic_ns")) is int
        and type(published) is int
        and event["monotonic_ns"] >= published
    ]
    consumed = [e for e in mine if e.get("event") == r7_fault.CONSUMED]
    if len(consumed) != 1:
        problems.append(
            f"the original lifetime consumed a False {len(consumed)} time(s) after "
            f"the fault was armed, not once"
        )
    elif returned is None or consumed[0]["monotonic_ns"] < returned:
        problems.append("the consumption came before the selected drain returned")
    elif not any(
        e.get("event") == r7_fault.HELD
        and e["monotonic_ns"] >= consumed[0]["monotonic_ns"]
        for e in mine
    ):
        problems.append("the original lifetime did not keep the lease after it")
    return problems
