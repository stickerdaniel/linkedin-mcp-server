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
from differential.test_row_judgement import _healthy
from linkedin_mcp_server import daemon_descriptor

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


def test_checkout_refusal_names_the_state():
    assert checkout_refusal({"head": PIN, "porcelain_empty": True}, PIN) is None
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
