"""The frozen baseline is refused unless it is its pin, and H-R12 reads as it must.

The checkout refusals run against a real throwaway git repository: a checkout
at another revision, and one with a change in it, are refused before any venv
is built. The interpreter rule runs on watcher records modelled on what the
watcher writes, and once through the real row entry with every launch
replaced. The H-R12 reading, ``!`` or ``=``, is judged from modelled row
observations. Nothing here builds a venv, installs a browser or starts one.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from differential import harness
from differential.baseline import (
    BaselineRefused,
    Runtime,
    checkout_refusal,
    interpreter_failures,
    prepare_baseline,
    verify_checkout,
)
from differential.harness import (
    RowResult,
    actor_environment,
    claim_account,
    coordination_reading,
    frozen_refusal,
    judge_row,
    k2_r12_verdict,
)
from differential import test_preservation_gate as gate
from differential import test_watcher as watcher_tests
from differential.test_row_judgement import _healthy
from differential.test_watcher import GRAMMAR, _shape
from differential.watcher import OWNER_MODULE
from linkedin_mcp_server import daemon_descriptor, process_tree

# The row entry with every launch replaced, and its staged profile.
row = gate.row
profile = gate.profile

# --- The checkout ------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path) -> tuple[Path, str, str]:
    """A repository with two commits; returns it, the first and the second."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "harness@example.invalid")
    _git(repo, "config", "user.name", "harness")
    _git(repo, "config", "commit.gpgsign", "false")
    shas = []
    for n in (1, 2):
        (repo / "file.txt").write_text(f"{n}\n")
        _git(repo, "add", "file.txt")
        _git(repo, "commit", "-q", "-m", f"commit {n}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return repo, shas[0], shas[1]


def test_a_clean_checkout_at_the_pin_is_accepted(repo):
    checkout, first, _ = repo
    _git(checkout, "checkout", "-q", "--detach", first)
    assert verify_checkout(checkout, first)["head"] == first


def test_a_checkout_at_another_revision_is_refused(repo):
    checkout, first, second = repo
    with pytest.raises(BaselineRefused, match="not the pinned"):
        verify_checkout(checkout, first)
    assert _git(checkout, "rev-parse", "HEAD") == second


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda c: (c / "file.txt").write_text("edited\n"), id="edited"),
        pytest.param(lambda c: (c / "stray.py").write_text("x = 1\n"), id="untracked"),
    ],
)
def test_a_dirty_checkout_is_refused(repo, change):
    checkout, first, _ = repo
    _git(checkout, "checkout", "-q", "--detach", first)
    change(checkout)
    with pytest.raises(BaselineRefused, match="not clean"):
        verify_checkout(checkout, first)


@pytest.mark.parametrize("dirty", [False, True], ids=["wrong-sha", "dirty"])
def test_preparing_refuses_an_existing_checkout_before_building_anything(
    repo, tmp_path, monkeypatch, dirty
):
    source, first, second = repo
    directory = tmp_path / "baseline"
    pin = first
    _git(
        source, "worktree", "add", "-q", "--detach", str(directory / "checkout"), first
    )
    if dirty:
        (directory / "checkout" / "stray.py").write_text("x = 1\n")
    else:
        pin = second
    ran: list[list[str]] = []
    real_run = subprocess.run

    def recording(command, *args, **kwargs):
        ran.append(list(command))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    with pytest.raises(BaselineRefused):
        prepare_baseline(directory, pinned=pin, repo=source)
    assert not [command for command in ran if command[:1] == ["uv"]]
    assert not [command for command in ran if "patchright" in command]


# --- The runtime identity ------------------------------------------------------------

PIN = "0" * 40


def _identity(checkout: Path, **changes: Any) -> dict[str, Any]:
    identity = {
        "checkout": str(checkout),
        "head": PIN,
        "porcelain_empty": True,
        "dirty_paths": [],
        "direct_url": {"url": checkout.as_uri(), "dir_info": {"editable": True}},
    }
    identity.update(changes)
    return identity


def _runtime(tmp_path: Path) -> Runtime:
    checkout = tmp_path / "baseline" / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    return Runtime(
        str(checkout / ".venv" / "bin" / "python"),
        checkout,
        tmp_path / "baseline" / "ms-playwright",
        PIN,
    )


def test_a_frozen_runtime_installed_from_its_own_checkout_is_accepted(tmp_path):
    runtime = _runtime(tmp_path)
    assert frozen_refusal(_identity(runtime.checkout), runtime) is None


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        ({"head": "1" * 40}, "not the pinned"),
        ({"porcelain_empty": False, "dirty_paths": [" M x"]}, "not clean"),
        (
            {
                "direct_url": {
                    "url": Path(harness.REPO_ROOT).as_uri(),
                    "dir_info": {"editable": True},
                }
            },
            "not from the checkout",
        ),
        ({"direct_url": None}, "not an editable install"),
    ],
    ids=["wrong-sha", "dirty", "installed-from-the-candidate", "no-direct-url"],
)
def test_a_frozen_runtime_that_is_not_its_pin_is_refused(tmp_path, changes, reported):
    runtime = _runtime(tmp_path)
    refusal = frozen_refusal(_identity(runtime.checkout, **changes), runtime)
    assert refusal is not None and reported in refusal


def test_a_checkout_whose_status_could_not_be_read_is_refused():
    # Unknown is not clean: a status that could not be read refuses the pin.
    assert checkout_refusal({"head": PIN, "porcelain_empty": None}, PIN)


# --- Which interpreter a baseline row ran --------------------------------------------

CANDIDATE_PREFIX = str(Path(sys.prefix))


def _start(actor: str, argv0: str, *, pid: int, in_row: bool = True) -> dict[str, Any]:
    module = (
        "linkedin_mcp_server.daemon_owner"
        if actor == "owner"
        else "linkedin_mcp_server"
    )
    return {
        "kind": "process.start",
        "actor": actor,
        "pid": pid,
        "in_row": in_row,
        "cmdline": [argv0, "-m", module],
    }


def _candidate_python() -> str:
    return str(Path(sys.prefix) / "bin" / "python")


def test_a_row_whose_actors_ran_the_baseline_passes(tmp_path):
    runtime = _runtime(tmp_path)
    records = [
        _start("frontend", runtime.python, pid=10),
        _start("owner", runtime.python, pid=11),
        # Not the row's, so whatever it runs is none of this row's business.
        _start("frontend", _candidate_python(), pid=12, in_row=False),
    ]
    assert (
        interpreter_failures(
            records, runtime, candidate_prefix=CANDIDATE_PREFIX, owner_expected=True
        )
        == []
    )


@pytest.mark.parametrize(
    ("records", "owner_expected", "reported"),
    [
        pytest.param(
            lambda b, c: [_start("frontend", c, pid=10)],
            False,
            "ran the candidate's interpreter",
            id="candidate-frontend",
        ),
        pytest.param(
            lambda b, c: [_start("frontend", b, pid=10), _start("owner", c, pid=11)],
            True,
            "the owner (pid 11) ran the candidate's interpreter",
            id="candidate-owner",
        ),
        pytest.param(
            lambda b, c: [_start("frontend", b, pid=10)],
            True,
            "no owner the watcher saw ran the baseline",
            id="owner-never-seen",
        ),
        pytest.param(
            lambda b, c: [], False, "no frontend the watcher saw", id="nothing-seen"
        ),
    ],
)
def test_a_baseline_row_that_ran_candidate_code_is_refused(
    tmp_path, records, owner_expected, reported
):
    runtime = _runtime(tmp_path)
    failures = interpreter_failures(
        records(runtime.python, _candidate_python()),
        runtime,
        candidate_prefix=CANDIDATE_PREFIX,
        owner_expected=owner_expected,
    )
    assert any(reported in failure for failure in failures), failures


#: The macOS runner's framework Python, as the first E1d packets recorded it
#: for every baseline frontend and owner: exe and argv[0] alike.
FRAMEWORK = (
    "/Library/Frameworks/Python.framework/Versions/3.13/Resources/"
    "Python.app/Contents/MacOS/Python"
)


def _framework(actor: str, launcher: str, *, pid: int) -> dict[str, Any]:
    return {**_start(actor, FRAMEWORK, pid=pid), "launcher": launcher}


def test_a_framework_build_is_identified_by_its_launcher(tmp_path):
    runtime = _runtime(tmp_path)
    records = [
        _framework("frontend", runtime.python, pid=10),
        _framework("owner", runtime.python, pid=11),
    ]
    assert (
        interpreter_failures(
            records, runtime, candidate_prefix=CANDIDATE_PREFIX, owner_expected=True
        )
        == []
    )
    # Without the launcher that is exactly what failed on the first run.
    bare = [_start("frontend", FRAMEWORK, pid=10)]
    failures = interpreter_failures(
        bare, runtime, candidate_prefix=CANDIDATE_PREFIX, owner_expected=False
    )
    assert any("no frontend the watcher saw" in f for f in failures)


@pytest.mark.parametrize(
    "records",
    [
        pytest.param(
            lambda b, c: [
                _framework("frontend", b, pid=10),
                _framework("owner", c, pid=11),
            ],
            id="candidate-owner-by-launcher",
        ),
        pytest.param(
            lambda b, c: [
                # Names the baseline in argv[0] and the candidate as launcher:
                # the candidate direction wins.
                {**_start("frontend", b, pid=10), "launcher": c},
                _framework("owner", b, pid=11),
            ],
            id="baseline-argv0-candidate-launcher",
        ),
    ],
)
def test_a_framework_candidate_is_still_refused(tmp_path, records):
    runtime = _runtime(tmp_path)
    failures = interpreter_failures(
        records(runtime.python, _candidate_python()),
        runtime,
        candidate_prefix=CANDIDATE_PREFIX,
        owner_expected=True,
    )
    assert any("ran the candidate's interpreter" in f for f in failures), failures


def test_the_same_interpreter_under_another_spelling_of_its_directory_counts(
    tmp_path,
):
    # macOS reaches a temporary directory as /var and as /private/var.
    real = tmp_path / "real"
    (real / "checkout" / ".venv" / "bin").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    python = str(real / "checkout" / ".venv" / "bin" / "python")
    runtime = Runtime(python, real / "checkout", real / "ms-playwright", PIN)
    record = _start(
        "frontend", str(alias / "checkout" / ".venv" / "bin" / "python"), pid=1
    )
    assert (
        interpreter_failures(
            [record], runtime, candidate_prefix=CANDIDATE_PREFIX, owner_expected=False
        )
        == []
    )


async def test_an_ineligible_row_never_reads_daemon_state_into_being(
    row, tmp_path, monkeypatch
):
    """The first E1d run's H-R12 K3 failure: the harness made the directory.

    ``daemon_descriptor.read`` prepares the daemon directory before it reads.
    Called on a row that must stay Direct, it created the state the row is
    then failed for. It may be called only once a descriptor exists.
    """
    reads: list[Any] = []
    descriptor = tmp_path / "not-yet-published.json"
    monkeypatch.setattr(
        harness.daemon_descriptor, "descriptor_path", lambda _root: descriptor
    )
    monkeypatch.setattr(
        harness.daemon_descriptor, "read", lambda root: reads.append(root)
    )
    await row(processes=[], summary={}, expect_owner=False)
    assert reads == []
    # The control: once one is published, the daemon row does read it.
    descriptor.write_text("{}")
    await row(processes=[], summary={})
    assert len(reads) == 1


async def test_the_row_fails_when_its_frozen_actors_ran_candidate_code(row, tmp_path):
    runtime = _runtime(tmp_path)
    clean = [
        {**_start("frontend", runtime.python, pid=10), "t": 1.0},
        {**_start("owner", runtime.python, pid=11), "t": 1.0},
    ]
    result, _ = await row(processes=[], summary={}, observed=clean, runtime=runtime)
    # The modelled row fails on its own account (a watcher summary from another
    # interval); only the interpreter rule is asked about here.
    assert result.runtime_failures == []
    assert not any("interpreter" in f for f in result.failures)
    leaked = [clean[0], _start("owner", _candidate_python(), pid=11)]
    result, _ = await row(processes=[], summary={}, observed=leaked, runtime=runtime)
    assert any("candidate's interpreter" in f for f in result.failures)
    assert any("candidate's interpreter" in f for f in result.runtime_failures)


def test_a_frozen_actor_environment_carries_no_foreign_code(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: home)
    monkeypatch.setenv("PYTHONPATH", str(harness.REPO_ROOT))
    monkeypatch.setenv("VIRTUAL_ENV", sys.prefix)
    monkeypatch.setenv("__PYVENV_LAUNCHER__", _candidate_python())
    (tmp_path / "auth").mkdir()
    account = claim_account(tmp_path / "auth" / "profile")
    env = actor_environment(
        account,
        "http://127.0.0.1:9",
        daemon=True,
        browsers=tmp_path / "b",
        chrome_path="/b/chrome",
    )
    assert "PYTHONPATH" not in env and "VIRTUAL_ENV" not in env
    assert "__PYVENV_LAUNCHER__" not in env
    assert env["CHROME_PATH"] == "/b/chrome"
    assert "CHROME_PATH" not in actor_environment(
        account, "http://127.0.0.1:9", daemon=True, browsers=tmp_path / "b"
    )


# --- H-R12: '!' and '=' ------------------------------------------------------------


def _r12(profile, *, forwarded: bool, owner: dict, state: bool, expect_owner: bool):
    healthy = _healthy(profile, daemon=True)
    stderr = ["INFO Forwarding to the shared browser owner"] if forwarded else []
    host = dataclasses.replace(healthy.host, stderr=stderr, user_lines=list(stderr))
    return dataclasses.replace(
        healthy,
        host=host,
        owner=owner,
        expect_owner=expect_owner,
        daemon_state_existed=state,
    )


_OWNER = {"pid": 4321, "exit": {"how": "exited"}}


def test_the_candidate_that_stays_direct_reads_equal_and_passes(profile):
    vector, failures = judge_row(
        _r12(profile, forwarded=False, owner={}, state=False, expect_owner=False)
    )
    assert failures == []
    assert coordination_reading(vector) == "="


@pytest.mark.parametrize(
    ("forwarded", "owner", "state", "reported"),
    [
        (True, {}, False, "reached a shared owner"),
        (False, {"descriptor_present": True}, True, "reached a shared owner"),
        (False, _OWNER, True, "reached a shared owner"),
        (False, {}, True, "left daemon state"),
    ],
    ids=["forwarded", "descriptor", "owner", "state-only"],
)
def test_the_candidate_that_coordinates_with_a_custom_browser_fails(
    profile, forwarded, owner, state, reported
):
    vector, failures = judge_row(
        _r12(profile, forwarded=forwarded, owner=owner, state=state, expect_owner=False)
    )
    assert any(reported in failure for failure in failures), failures


def _k2(profile, *, forwarded: bool, owner: dict, **changes) -> RowResult:
    observed = _r12(
        profile, forwarded=forwarded, owner=owner, state=bool(owner), expect_owner=True
    )
    vector, failures = judge_row(observed)
    return RowResult(
        "K2",
        "daemon",
        vector=vector,
        host=observed.host,
        cleanup=observed.cleanup,
        failures=failures,
        **changes,
    )


def _reading(result: RowResult) -> str:
    assert result.vector is not None
    return coordination_reading(result.vector)


def test_k2_reading_bang_passes_whatever_else_the_baseline_did(profile):
    result = _k2(profile, forwarded=True, owner=_OWNER)
    assert _reading(result) == "!"
    assert k2_r12_verdict(result) == []
    # An owner published without forwarding is still coordination, and so is
    # forwarding to an owner whose descriptor the row never got to read.
    for forwarded, owner in ((False, _OWNER), (True, {})):
        result = _k2(profile, forwarded=forwarded, owner=owner)
        assert _reading(result) == "!"
        assert k2_r12_verdict(result) == []


def test_k2_reading_equal_is_a_harness_defect(profile):
    result = _k2(profile, forwarded=False, owner={})
    assert _reading(result) == "="
    (problem,) = k2_r12_verdict(result)
    assert "harness defect" in problem


def test_k2_counts_an_owner_it_started_even_unpublished(profile):
    result = _k2(profile, forwarded=False, owner={})
    assert result.vector is not None
    result.vector = dataclasses.replace(result.vector, owner_launched=True)
    assert _reading(result) == "!"
    assert k2_r12_verdict(result) == []


# --- H-R12 through the row entry: an owner the watcher saw start ----------------

_OWNER_ARGV = ["/venv/bin/python", "-P", "-m", "linkedin_mcp_server.daemon_owner"]


def _owner_event(kind: str, *, in_row: bool, cmdline=_OWNER_ARGV) -> dict[str, Any]:
    return {
        "kind": kind,
        "actor": "owner",
        "pid": 77,
        "start_identity": 5.0,
        "in_row": in_row,
        "cmdline": list(cmdline) if in_row else [],
    }


@pytest.fixture
def direct_row(row, tmp_path, monkeypatch):
    """The row entry for a row that must stay Direct: no descriptor, no state."""
    monkeypatch.setattr(
        harness.daemon_descriptor,
        "descriptor_path",
        lambda _root: tmp_path / "never-published.json",
    )
    monkeypatch.setattr(
        harness,
        "retire_daemon_state",
        lambda *a: harness.DaemonCleanup("dir", False, False, True, True),
    )
    # And a frontend that never logged forwarding.
    forwarding_host = harness.run_host_session

    async def direct_host(*args, **kwargs):
        session = await forwarding_host(*args, **kwargs)
        return dataclasses.replace(session, stderr=[], user_lines=[])

    monkeypatch.setattr(harness, "run_host_session", direct_host)

    async def run(observed):
        result, _ = await row(
            processes=[], summary={}, observed=observed, expect_owner=False
        )
        return result

    return run


_OWNER_FAILURE = "started a shared owner process"


@pytest.mark.parametrize(
    "observed",
    [
        pytest.param([], id="no-owner-event"),
        pytest.param(
            [
                _owner_event("process.start", in_row=False),
                _owner_event("process.exit", in_row=False),
            ],
            id="unrelated-owner",
        ),
        pytest.param(
            # Attribution is ancestry, not the watcher's withholding of the
            # arguments outside the row.
            [{**_owner_event("process.start", in_row=False), "cmdline": _OWNER_ARGV}],
            id="unrelated-owner-with-its-arguments",
        ),
        pytest.param(
            [
                {
                    **_owner_event("process.start", in_row=True),
                    # Names the module inside an argument, not as the module.
                    "cmdline": [
                        "/venv/bin/python",
                        "helper.py",
                        "--log=/tmp/linkedin_mcp_server.daemon_owner.log",
                    ],
                    "actor": "owner",
                }
            ],
            id="row-process-merely-naming-the-owner",
        ),
    ],
)
async def test_a_direct_row_without_an_owner_launch_reads_equal(direct_row, observed):
    result = await direct_row(observed)
    assert result.vector is not None
    assert not result.vector.owner_launched
    assert not any(_OWNER_FAILURE in failure for failure in result.failures)
    assert coordination_reading(result.vector) == "="


@pytest.mark.parametrize(("arguments", "runs"), GRAMMAR)
def test_an_owner_launch_is_read_with_the_interpreters_grammar(arguments, runs):
    record = {
        **_owner_event("process.start", in_row=True),
        "cmdline": ["/venv/bin/python", *_shape(arguments, OWNER_MODULE)],
    }
    assert harness.owner_launches([record]) == ([77] if runs else [])


@pytest.mark.parametrize(
    ("arguments", "fails"),
    [
        pytest.param(["-Pm", "M"], True, id="clustered-owner"),
        pytest.param(["-cpass", "-m", "M"], False, id="dash-c-then-owner-words"),
        pytest.param(["--", "-m", "M"], False, id="double-dash-then-owner-words"),
    ],
)
async def test_the_row_reads_owner_launches_with_the_grammar(
    direct_row, arguments, fails
):
    observed = [
        {
            **_owner_event("process.start", in_row=True),
            "cmdline": ["/venv/bin/python", *_shape(arguments, OWNER_MODULE)],
        }
    ]
    result = await direct_row(observed)
    assert result.vector is not None
    assert result.vector.owner_launched is fails
    assert any(_OWNER_FAILURE in f for f in result.failures) is fails


# --- The Windows owner release gate (review e1db, E1DB-02) ----------------------

_NONCE = "ab" * 32
_OWNER_TARGET = [sys.executable, "-P", "-m", OWNER_MODULE, "--job-name", "Local\\j"]


def _gate(**changes: Any) -> list[str]:
    """What the product's own builder makes for the owner, never executed."""
    command = process_tree.windows_gate_command(list(_OWNER_TARGET), _NONCE)
    for index, value in changes.items():
        command[int(index[1:])] = value
    return command


def _gate_event(cmdline: list[str], *, in_row: bool = True) -> dict[str, Any]:
    return {
        "kind": "process.start",
        "actor": "other",
        "pid": 88,
        "start_identity": 4.0,
        "in_row": in_row,
        "cmdline": list(cmdline),
    }


_GATE_FAILURE = "started the shared owner's release gate"


def test_the_gate_names_this_runtimes_own_script():
    command = _gate()
    assert harness.owner_gate(command, [harness.gate_script(harness.REPO_ROOT)])
    # Another runtime's gate script, at the same shape, is someone else's.
    assert not harness.owner_gate(command, [harness.gate_script(Path("/elsewhere"))])


def test_a_baseline_runtimes_gate_is_recognised_by_its_own_path(tmp_path):
    checkout = tmp_path / "baseline" / "checkout"
    script = harness.gate_script(checkout)
    script.parent.mkdir(parents=True)
    script.write_text("")
    command = [sys.executable, "-I", "-S", "-u", str(script), _NONCE, "--"]
    command += _OWNER_TARGET
    assert harness.owner_gate(command, [script])
    assert not harness.owner_gate(command, [harness.gate_script(harness.REPO_ROOT)])


async def test_a_gate_alone_is_an_owner_start_attempt(direct_row):
    # The gate never released its target: no owner process, no descriptor.
    result = await direct_row([_gate_event(_gate())])
    assert result.vector is not None
    assert result.vector.owner_start_attempted
    assert not result.vector.owner_launched and not result.vector.owner_published
    assert any(_GATE_FAILURE in f for f in result.failures)
    assert coordination_reading(result.vector) == "!"


async def test_a_released_gate_and_its_owner_both_count(direct_row):
    owner = {**_owner_event("process.start", in_row=True), "cmdline": _OWNER_TARGET}
    result = await direct_row([_gate_event(_gate()), owner])
    assert result.vector is not None
    assert result.vector.owner_start_attempted and result.vector.owner_launched
    assert any(_GATE_FAILURE in f for f in result.failures)
    assert any(_OWNER_FAILURE in f for f in result.failures)


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            _gate_event(_gate(i4=str(Path("/tmp/process_gate.py")))),
            id="wrong-script-path",
        ),
        pytest.param(
            _gate_event(_gate(i4=str(harness.REPO_ROOT / "process_gate.py"))),
            id="script-outside-the-package",
        ),
        pytest.param(_gate_event(_gate(i5="not-a-nonce")), id="bad-nonce"),
        pytest.param(_gate_event(_gate(i6="-x")), id="no-separator"),
        pytest.param(_gate_event(_gate(i1="-E")), id="other-interpreter-flags"),
        pytest.param(
            _gate_event(_gate(i9="linkedin_mcp_server")), id="target-not-the-owner"
        ),
        pytest.param(
            _gate_event([*_gate()[:7], sys.executable, "-cpass", "-m", OWNER_MODULE]),
            id="target-runs-inline-code",
        ),
        pytest.param(_gate_event(_gate()[:6]), id="truncated"),
        pytest.param(_gate_event(_gate(), in_row=False), id="unrelated-gate"),
        pytest.param(
            _gate_event(
                [
                    sys.executable,
                    "helper.py",
                    f"--note={' '.join(_gate())}",
                ]
            ),
            id="gate-named-inside-an-argument",
        ),
    ],
)
async def test_what_is_not_this_rows_owner_gate_is_no_attempt(direct_row, event):
    result = await direct_row([event])
    assert result.vector is not None
    assert not result.vector.owner_start_attempted
    assert not any(_GATE_FAILURE in f for f in result.failures)
    assert coordination_reading(result.vector) == "="


def test_k2_counts_a_gate_alone(profile):
    result = _k2(profile, forwarded=False, owner={})
    assert result.vector is not None
    result.vector = dataclasses.replace(result.vector, owner_start_attempted=True)
    assert _reading(result) == "!"
    assert k2_r12_verdict(result) == []


def test_a_gate_is_never_asked_for_its_environment():
    table: dict[int, dict[str, Any]] = {
        1: {"start": 1.0, "ppid": 0, "cmdline": ["pytest"]},
    }
    sampler = watcher_tests._sampler(table)
    sampler.sample()
    table[2] = {
        "start": 2.0,
        "ppid": 1,
        "cmdline": _gate(),
        "environ": {"__PYVENV_LAUNCHER__": "/x/.venv/bin/python"},
    }
    for _ in range(3):
        sampler.sample()
    assert "environ_reads" not in table[2]


@pytest.mark.parametrize("kind", ["process.start", "process.update"])
async def test_a_direct_row_that_started_an_unpublished_owner_fails(direct_row, kind):
    # Started and gone before it published: no descriptor, no forwarding line,
    # no daemon state left, only the watcher's record of the start.
    observed = [
        _owner_event(kind, in_row=True),
        _owner_event("process.exit", in_row=True),
    ]
    result = await direct_row(observed)
    assert result.vector is not None
    assert result.vector.owner_launched and not result.vector.owner_published
    assert any(_OWNER_FAILURE in failure for failure in result.failures)
    assert coordination_reading(result.vector) == "!"


@pytest.mark.parametrize(
    ("changes", "reported"),
    [
        (
            {"runtime_failures": ["the owner ran the candidate's interpreter"]},
            "candidate",
        ),
        ({"vector": None}, "no vector"),
    ],
    ids=["candidate-code", "no-vector"],
)
def test_k2_that_cannot_stand_for_the_baseline_is_refused(profile, changes, reported):
    result = dataclasses.replace(_k2(profile, forwarded=True, owner=_OWNER), **changes)
    assert any(reported in problem for problem in k2_r12_verdict(result))


def test_k2_whose_host_never_ran_reads_nothing(profile):
    result = _k2(profile, forwarded=False, owner={})
    assert result.host is not None
    result.host = dataclasses.replace(result.host, error="TimeoutError: init")
    problems = k2_r12_verdict(result)
    assert any("could not be read" in problem for problem in problems)
    assert not any("harness defect" in problem for problem in problems)
