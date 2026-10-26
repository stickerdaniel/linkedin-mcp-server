"""K2's source-model witnesses: the frozen pin's own strict-xfail tests, run at
the pin and judged from what each one raised.

Release gate 1 asks every negative control to reproduce its regression
witness. W-UNMARKED and W-OWNER-UNMARKED have no native row; their witnesses
are the tests the pin itself carries in ``tests/test_daemon_regression_witnesses.py``.
They are run unchanged, from the pin's checkout, by the pin's interpreter with
its locked dev dependencies (``baseline.prepare_frozen_tests``), and nothing
of this checkout is imported into that run except ``witness_probe``, a plugin
that only records.

**Reproduced means the intended assertion failed.** Not any
``AssertionError``, not pytest's exit status and not the xfail verdict alone:
a node reproduces its witness only when its call was reported as the declared
xfail, setup and teardown passed, and the exception it raised is an
``AssertionError`` raised by the declared statement on the declared line of
the declared test. A strict XPASS, an ``AssertionError`` from anywhere else,
a ``pytest.fail`` reachability check, a missing node, or a run that imported
another checkout's package is *not reproduced*, with the reason. A witness is
reproduced only when every node it names is.

Every record says ``source-model``: the real middleware runs against doubles,
so none of it is a native measurement and none of it fills a native cell.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from differential.baseline import Runtime

#: The witnesses' file at the pin, relative to its checkout.
WITNESS_FILE = "tests/test_daemon_regression_witnesses.py"
PROBE = Path(__file__).with_name("witness_probe.py")
#: What the judgement is written as, beside ``counts.json`` in the packet.
WITNESSES_FILE = "witnesses.json"
EVIDENCE = "source-model"


@dataclass(frozen=True)
class Witness:
    """One regression witness: the pin's test, its nodes, and the assertion
    whose failure is the regression."""

    name: str
    test: str
    #: Each node the test runs at the pin, parameters included.
    nodes: tuple[str, ...]
    #: The assertion whose failure is the witness, as the pin's source has it.
    statement: str
    line: int


def _nodes(test: str, *parameters: str) -> tuple[str, ...]:
    base = f"{WITNESS_FILE}::{test}"
    return tuple(f"{base}[{p}]" for p in parameters) if parameters else (base,)


#: The two the plan runs at the pin (gate 1, Stage 1). The lines are the
#: pin's, which never changes.
WITNESSES: dict[str, Witness] = {
    witness.name: witness
    for witness in (
        Witness(
            name="W-UNMARKED",
            test="test_a_failed_heartbeat_preflight_dispatches_no_tool_call",
            nodes=_nodes(
                "test_a_failed_heartbeat_preflight_dispatches_no_tool_call",
                "raises",
                "404",
            ),
            statement="assert dispatched == []",
            line=429,
        ),
        Witness(
            name="W-OWNER-UNMARKED",
            test="test_the_owner_refuses_a_call_without_a_marker",
            nodes=_nodes("test_the_owner_refuses_a_call_without_a_marker"),
            statement="assert dispatched == []",
            line=472,
        ),
    )
}

REPRODUCED = "reproduced"
NOT_REPRODUCED = "not reproduced"


def run_witnesses(
    runtime: Runtime,
    report: Path,
    witnesses: Sequence[Witness] = tuple(WITNESSES.values()),
    *,
    timeout: float = 600,
) -> dict[str, Any]:
    """Run *witnesses* in *runtime*'s checkout with its interpreter, and return
    what ``witness_probe`` recorded. Whether that checkout is the pin is the
    caller's to verify, before and after (``baseline.verify_checkout``).

    The probe is copied into a directory of its own for ``PYTHONPATH``, so the
    run can import it and nothing else of this checkout.
    """
    report.parent.mkdir(parents=True, exist_ok=True)
    # A Windows file still held at cleanup must not turn a verdict into an error.
    with tempfile.TemporaryDirectory(
        prefix="witness-probe-", ignore_cleanup_errors=True
    ) as scratch:
        plugins = Path(scratch) / "plugins"
        plugins.mkdir()
        shutil.copyfile(PROBE, plugins / PROBE.name)
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME")
        }
        env.update(PYTHONPATH=str(plugins), PYTHONDONTWRITEBYTECODE="1")
        completed = subprocess.run(
            [
                runtime.python,
                "-m",
                "pytest",
                "-p",
                PROBE.stem,
                f"--witness-report={report}",
                "-p",
                "no:cacheprovider",
                f"--basetemp={Path(scratch) / 'basetemp'}",
                *(node for witness in witnesses for node in witness.nodes),
            ],
            cwd=runtime.checkout,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    if not report.is_file():
        return {
            "exitstatus": completed.returncode,
            "phases": [],
            "output": (completed.stdout + completed.stderr)[-4000:],
        }
    recorded = json.loads(report.read_text(encoding="utf-8"))
    recorded["output"] = completed.stdout[-4000:]
    return recorded


def _within(path: str | None, root: Path) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def judge_node(
    witness: Witness, node: str, phases: Sequence[Mapping[str, Any]]
) -> list[str]:
    """Why *node* does not reproduce *witness*; empty when it does."""
    by_phase = {phase["when"]: phase for phase in phases if phase["nodeid"] == node}
    if not by_phase:
        return [f"{node} did not run at the pin"]
    reasons = [
        f"{node} {when} {by_phase[when]['outcome']}"
        for when in ("setup", "teardown")
        if by_phase.get(when, {}).get("outcome") != "passed"
    ]
    call = by_phase.get("call")
    if call is None:
        return [*reasons, f"{node} never reached its call"]
    if call["outcome"] != "skipped" or not call.get("wasxfail"):
        # A strict XPASS reports a failed call that raised nothing.
        reasons.append(
            f"{node} was not the declared xfail: its call {call['outcome']}"
            + ("" if call.get("raised") else " without raising")
        )
    raised = call.get("raised")
    if raised is None:
        return [*reasons, f"{node} raised nothing, so its assertion held"]
    expected = (
        "builtins.AssertionError",
        WITNESS_FILE,
        witness.test,
        witness.line,
        witness.statement,
    )
    seen = (
        raised.get("type"),
        raised.get("path"),
        raised.get("function"),
        raised.get("line"),
        raised.get("statement"),
    )
    if seen != expected:
        reasons.append(
            f"{node} raised {raised.get('type')} from {raised.get('path')}:"
            f"{raised.get('line')} in {raised.get('function')} "
            f"({raised.get('statement')!r}), not the intended "
            f"{witness.statement!r} at {WITNESS_FILE}:{witness.line}"
        )
    return reasons


def judge(
    recorded: Mapping[str, Any],
    checkout: Path,
    witnesses: Sequence[Witness] = tuple(WITNESSES.values()),
) -> dict[str, dict[str, Any]]:
    """Each witness's verdict, with every node's and the reasons for a miss."""
    phases = recorded.get("phases", [])
    foreign = (
        []
        if _within(recorded.get("package_file"), checkout)
        else [
            f"the run imported linkedin_mcp_server from "
            f"{recorded.get('package_file')}, not from the pin's checkout {checkout}"
        ]
    )
    verdicts: dict[str, dict[str, Any]] = {}
    for witness in witnesses:
        nodes = []
        for node in witness.nodes:
            reasons = [*foreign, *judge_node(witness, node, phases)]
            call = next(
                (p for p in phases if p["nodeid"] == node and p["when"] == "call"),
                None,
            )
            nodes.append(
                {
                    "node": node,
                    "verdict": NOT_REPRODUCED if reasons else REPRODUCED,
                    "reasons": reasons,
                    "raised": call.get("raised") if call else None,
                }
            )
        verdicts[witness.name] = {
            "witness": witness.name,
            "evidence": EVIDENCE,
            "intended": f"{WITNESS_FILE}:{witness.line} {witness.statement}",
            "verdict": (
                REPRODUCED
                if all(n["verdict"] == REPRODUCED for n in nodes)
                else NOT_REPRODUCED
            ),
            "nodes": nodes,
        }
    return verdicts


def write_witnesses(
    directory: Path,
    runtime: Runtime,
    recorded: Mapping[str, Any],
    verdicts: Mapping[str, Any],
) -> Path:
    """The packet's ``witnesses.json``: the pin, the run and every verdict."""
    path = directory / WITNESSES_FILE
    path.write_text(
        json.dumps(
            {
                "pinned": runtime.pinned,
                "interpreter": runtime.python,
                "pytest": recorded.get("pytest"),
                "package_file": recorded.get("package_file"),
                "exitstatus": recorded.get("exitstatus"),
                # The pin's pytest output, last lines, for a run that went wrong.
                "output": recorded.get("output"),
                "witnesses": verdicts,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
