"""The pilot recorder, ``scripts/daemon_pilot.py``.

Frontends run the production startup path with only the election and the
transport stubbed, so the decision lines the recorder reads are the ones
``cli_main`` writes. ``--status`` runs through a stand-in candidate that
prints the CLI's own lines, and the owner is a real descriptor beside a
``daemon.log`` shaped like the one a probe of the owner wrote.
"""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.metadata
import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import AsyncMock, patch

import pytest

from linkedin_mcp_server import cli_main, debug_trace
from linkedin_mcp_server.config import set_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import descriptor, discovery, election, proxy
from linkedin_mcp_server.daemon.discovery import OwnerState
from linkedin_mcp_server.exceptions import BrowserBusyError
from linkedin_mcp_server.session_state import (
    QUARANTINE_PREFIX,
    portable_cookie_path,
    write_source_state,
)

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "daemon_pilot.py"
_SPEC = importlib.util.spec_from_file_location("daemon_pilot", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
pilot = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pilot)

_VERSION = importlib.metadata.version("mcp-server-linkedin")
_FORWARD = SimpleNamespace(
    worth_connecting=True, attachment_lookup=SimpleNamespace(attachment=object())
)
_ALONE = SimpleNamespace(
    worth_connecting=False,
    attachment_lookup=SimpleNamespace(attachment=None, state=OwnerState.ABSENT),
    unanswered=None,
)


def _log_line(logger: str, message: str) -> str:
    return json.dumps(
        {
            "timestamp": "2026-10-15 08:40:34,530",
            "level": "INFO",
            "logger": logger,
            "message": message,
        }
    )


_READ = _log_line("linkedin_mcp_server.tools.feed", "Reading feed (num_posts=1)")
_STATUS_ERR = "\n".join(
    _log_line("linkedin_mcp_server.cli_main", line)
    for line in (
        f"LinkedIn MCP Server v{_VERSION}",
        f"LinkedIn MCP Server v{_VERSION} - Session Info mode",
    )
)


def _valid(profile: Path) -> dict[str, Any]:
    out = (
        f"Profile mode: source ({profile})\n✅ Session is valid (profile: {profile})\n"
    )
    return {"exit": 0, "stdout": out, "stderr": _STATUS_ERR, "profile": str(profile)}


def _no_session(profile: Path) -> dict[str, Any]:
    out = f"❌ No valid source session found at {profile}\n"
    return {"exit": 1, "stdout": out, "stderr": _STATUS_ERR, "profile": str(profile)}


# --- Fixtures ---------------------------------------------------------------


def _start_frontend(
    monkeypatch: pytest.MonkeyPatch,
    profile: Path,
    outcome: SimpleNamespace | None,
    *,
    user_data_dir: Path | None = None,
    log_level: Literal["INFO", "WARNING"] = "INFO",
    transport_fails: bool = False,
) -> None:
    """Start one frontend through ``cli_main.main``; *outcome* None means no daemon."""
    monkeypatch.setenv("LINKEDIN_TRACE_MODE", "always")
    config = AppConfig()
    config.browser.user_data_dir = str(user_data_dir or profile)
    config.server.daemon_enabled = outcome is not None
    config.server.log_level = log_level
    config.server.transport = "stdio"
    config.is_interactive = False

    def run(**_kwargs: Any) -> None:
        if transport_fails:
            raise RuntimeError("the host closed the connection")

    server = SimpleNamespace(run=run)
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(cli_main, "load_config", return_value=config))
        stack.enter_context(patch.object(cli_main, "configure_browser_environment"))
        stack.enter_context(
            patch.object(cli_main, "create_mcp_server", return_value=server)
        )
        if outcome is not None:
            stack.enter_context(
                patch.object(discovery, "daemon_would_be_used", return_value=True)
            )
        stack.enter_context(
            patch.object(election, "obtain_owner", return_value=outcome)
        )
        stack.enter_context(patch.object(proxy, "DaemonProxyBackend"))
        with contextlib.suppress(SystemExit):
            cli_main.main([])
    # The next frontend is another process with a trace run of its own.
    debug_trace.reset_trace_state_for_testing()


def _exits_before_deciding(monkeypatch: pytest.MonkeyPatch, profile: Path) -> None:
    """A frontend refused on an unowned root, which ends before any election."""
    foreign = profile.parent / "foreign"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("not ours")
    _start_frontend(monkeypatch, profile, _FORWARD, user_data_dir=foreign / "profile")


def _store(profile: Path) -> None:
    profile.mkdir(exist_ok=True)
    (profile / "Last Version").write_text("146.0.7680.31")
    portable_cookie_path(profile).write_text(
        json.dumps([{"name": "li_at", "value": "a"}])
    )
    write_source_state(profile)


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def _publish_owner(profile: Path, pid: int, lines: list[str]) -> Path:
    """A descriptor as an owner writes it, and the log beside it."""
    directory = descriptor.daemon_state_root() / ("ab" * 32)
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / "daemon.log"
    built = descriptor.build(
        instance_id=descriptor.new_instance_id(),
        package_version=_VERSION,
        runtime_id="test-host",
        profile=profile,
        host="127.0.0.1",
        port=12345,
        path="/mcp",
        token="synthetic-token",
        config=AppConfig(),
        log_path=log,
    )
    (directory / "daemon.json").write_text(
        dataclasses.replace(built, pid=pid).to_json()
    )
    log.write_text("".join(f"{line}\n" for line in lines))
    return log


def _command_line(*args: str) -> str:
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


class _Candidate:
    """A stand-in for the uvx command, answering ``--status`` from a script.

    Like the real one, it finds a session only in the profile it is given.
    """

    def __init__(self, folder: Path) -> None:
        self._steps = folder / "steps.json"
        self._steps.write_text("[]")
        script = folder / "candidate.py"
        script.write_text(
            "import json, os, pathlib, sys\n"
            "steps = pathlib.Path(sys.argv[1])\n"
            "plan = json.loads(steps.read_text())\n"
            "step = plan.pop(0)\n"
            "steps.write_text(json.dumps(plan))\n"
            "args = sys.argv[2:]\n"
            "flag = '--user-data-dir'\n"
            "given = args[args.index(flag) + 1] if flag in args else ''\n"
            "if os.path.realpath(given) != os.path.realpath(step['profile']):\n"
            "    out = f'No valid source session found at {given}\\n'\n"
            "    step = {'exit': 1, 'stdout': out, 'stderr': ''}\n"
            "if step.get('trace_run'):\n"
            "    run = pathlib.Path(step['trace_run'])\n"
            "    run.mkdir(parents=True)\n"
            "    (run / 'server.log').write_text(step['stderr'] + '\\n')\n"
            "sys.stdout.buffer.write(step['stdout'].encode())\n"
            "sys.stderr.buffer.write(step['stderr'].encode())\n"
            "sys.exit(step['exit'])\n"
        )
        self.command = _command_line(sys.executable, str(script), str(self._steps))

    def answer(self, *steps: dict[str, Any]) -> None:
        self._steps.write_text(json.dumps(list(steps)))


@pytest.fixture
def pilot_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("pilot")


@pytest.fixture
def candidate(tmp_path_factory: pytest.TempPathFactory) -> _Candidate:
    return _Candidate(tmp_path_factory.mktemp("candidate"))


def _before(pilot_dir: Path, candidate: _Candidate, profile: Path) -> tuple[int, str]:
    code = pilot.main(
        [
            "before",
            "--pilot-dir",
            str(pilot_dir),
            "--candidate",
            candidate.command,
            "--host",
            "test-host",
            "--others-stopped",
            "--user-data-dir",
            str(profile),
        ]
    )
    sessions = (
        sorted(path.name for path in pilot_dir.iterdir()) if pilot_dir.exists() else []
    )
    return code, sessions[-1] if sessions else ""


def _after(pilot_dir: Path, session: str, *attest: str) -> int:
    extra = [arg for entry in attest for arg in ("--attest", entry)]
    return pilot.main(
        ["after", "--pilot-dir", str(pilot_dir), "--session", session, *extra]
    )


def _record(pilot_dir: Path, session: str) -> dict[str, Any]:
    return json.loads((pilot_dir / session / "after.json").read_text())


def _run_session(
    pilot_dir: Path,
    candidate: _Candidate,
    profile: Path,
    during: Any,
    *attest: str,
) -> dict[str, Any]:
    """before, the host session *during* describes, then after."""
    candidate.answer(_valid(profile), _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0
    during()
    assert _after(pilot_dir, session, *attest) == 0
    return _record(pilot_dir, session)


# --- Mode of a session -------------------------------------------------------


@pytest.mark.parametrize(
    ("frontends", "mode"),
    [
        (["forward"], "daemon"),
        (["forward", "forward"], "daemon"),
        (["alone"], "fallback"),
        (["forward", "alone"], "fallback"),
        (["forward", "undecided"], "uncertain"),
        (["alone", "exits"], "uncertain"),
        # Below INFO a forwarding frontend may log nothing, or only an error.
        (["forward", "quiet-forward"], "uncertain"),
        (["forward", "quiet-broken"], "uncertain"),
        (["forward", "quiet-alone"], "fallback"),
        # The owner's run repeats its log and decides nothing; the same lines
        # outside the owner log are some other process.
        (["forward", "owner"], "daemon"),
        (["forward", "stray"], "uncertain"),
        ([], "uncertain"),
    ],
)
def test_a_session_takes_its_mode_from_every_frontend(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    frontends: list[str],
    mode: str,
) -> None:
    profile = isolate_profile_dir
    owner_log: list[str] = []
    kinds = []
    outcomes = {"forward": _FORWARD, "alone": _ALONE, "undecided": None}
    for frontend in frontends:
        if frontend == "exits":
            _exits_before_deciding(monkeypatch, profile)
        elif frontend in ("owner", "stray"):
            run = profile.parent / "trace-runs" / f"run-{frontend}"
            run.mkdir(parents=True)
            starting = _log_line(
                "linkedin_mcp_server.server", "LinkedIn MCP Server starting..."
            )
            (run / "server.log").write_text(f"{starting}\n{_READ}\n")
            if frontend == "owner":
                owner_log += [starting, "Uvicorn running on 127.0.0.1", _READ]
        else:
            quiet = frontend.startswith("quiet-")
            _start_frontend(
                monkeypatch,
                profile,
                outcomes[
                    "forward"
                    if frontend == "quiet-broken"
                    else frontend.removeprefix("quiet-")
                ],
                log_level="WARNING" if quiet else "INFO",
                transport_fails=frontend == "quiet-broken",
            )
        kinds.append(
            {"owner": "owner", "quiet-forward": "unknown"}.get(frontend, "frontend")
        )

    runs = pilot.copy_trace_runs(
        profile.parent, [], tmp_path_factory.mktemp("evidence"), owner_log
    )

    assert sorted(run["kind"] for run in runs) == sorted(kinds)
    assert pilot.session_mode(runs) == mode


# --- --status ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("runtime", "browser", "verdict"),
    [
        (
            "native",
            AsyncMock(return_value=SimpleNamespace(is_authenticated=True)),
            "valid",
        ),
        (
            "native",
            AsyncMock(return_value=SimpleNamespace(is_authenticated=False)),
            "auth-failed",
        ),
        ("native", AsyncMock(side_effect=BrowserBusyError()), "busy"),
        # Exits 0 after saying the cookies were not checked.
        ("foreign", AsyncMock(), "not-verified"),
    ],
)
def test_the_status_verdict_reads_the_cli_s_own_output(
    capsys: pytest.CaptureFixture[str],
    isolate_profile_dir: Path,
    runtime: str,
    browser: AsyncMock,
    verdict: str,
) -> None:
    _store(isolate_profile_dir)
    set_config(AppConfig())
    current = "foreign-runtime" if runtime == "foreign" else cli_main.get_runtime_id()
    with (
        patch.object(cli_main, "get_runtime_id", return_value=current),
        patch.object(cli_main, "get_or_create_browser", browser),
        patch.object(cli_main, "close_browser", AsyncMock()),
        pytest.raises(SystemExit) as exited,
    ):
        cli_main.profile_info_and_exit()

    code = exited.value.code
    assert isinstance(code, int)
    assert pilot.classify_status(code, capsys.readouterr().out) == verdict


# The real `--status` of an authenticated session, as a candidate process.
_REAL_STATUS = """
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from linkedin_mcp_server import cli_main
from linkedin_mcp_server.config import set_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.session_state import portable_cookie_path, write_source_state

profile = Path(sys.argv[sys.argv.index("--user-data-dir") + 1])
profile.mkdir(parents=True)
(profile / "Last Version").write_text("146.0.7680.31")
portable_cookie_path(profile).write_text("[]")
write_source_state(profile)
config = AppConfig()
config.browser.user_data_dir = str(profile)
config.is_interactive = False
set_config(config)
browser = AsyncMock(return_value=SimpleNamespace(is_authenticated=True))
with (
    patch.object(cli_main, "get_or_create_browser", browser),
    patch.object(cli_main, "close_browser", AsyncMock()),
):
    cli_main.profile_info_and_exit()
"""


def test_the_status_survives_a_console_without_the_check_mark(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # What a Windows console gives Python by default.
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    script = tmp_path / "status.py"
    script.write_text(_REAL_STATUS)

    status = pilot.run_status(
        _command_line(sys.executable, str(script)), tmp_path / "auth" / "profile"
    )

    assert status["verdict"] == "valid"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_an_interrupt_after_the_reap_signals_no_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class ReapedThenInterrupted(subprocess.Popen):
        """Ctrl-C lands just after ``communicate`` reaped the leader."""

        interrupted = False

        def _communicate(self, *args: Any, **kwargs: Any) -> Any:
            # CPython's private step that reads the pipes, then reaps.
            result = getattr(super(), "_communicate")(*args, **kwargs)
            if not self.interrupted:
                self.interrupted = True
                raise KeyboardInterrupt
            return result

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(pilot.subprocess, "Popen", ReapedThenInterrupted)
    monkeypatch.setattr(pilot.os, "killpg", lambda *call: signals.append(call))

    with pytest.raises(KeyboardInterrupt):
        pilot._supervised([sys.executable, "-c", "pass"])

    assert signals == []
    assert "may still be running" in capsys.readouterr().err


def test_a_status_timeout_ends_everything_the_candidate_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    beat = tmp_path / "beat"
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import pathlib, sys, time\n"
        "beat = pathlib.Path(sys.argv[1])\n"
        "deadline = time.monotonic() + 30\n"
        "count = 0\n"
        "while time.monotonic() < deadline:\n"
        "    count += 1\n"
        "    beat.write_text(str(count))\n"
        "    time.sleep(0.05)\n"
    )
    # Like uvx, which runs the candidate as a child of its own.
    launcher = tmp_path / "launcher.py"
    launcher.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(30)\n"
    )
    # Long enough for three interpreters to start on a slow Windows runner.
    monkeypatch.setattr(pilot, "_STATUS_SECONDS", 5)

    status = pilot.run_status(
        _command_line(sys.executable, str(launcher), str(worker), str(beat)),
        tmp_path / "profile",
    )

    assert (status["verdict"], status["timed_out"], status["tree_ended"]) == (
        "error",
        True,
        True,
    )
    assert beat.exists(), "the worker never started, so nothing was proved"
    seen = beat.read_text()
    time.sleep(0.5)
    assert beat.read_text() == seen


# --- One session, before to after ------------------------------------------


def test_a_forwarding_session_with_a_read_qualifies_and_is_retained(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [_READ])  # an earlier session's read
    status_run = profile.parent / "trace-runs" / "run-status"
    candidate.answer({**_valid(profile), "trace_run": str(status_run)}, _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0

    _start_frontend(monkeypatch, profile, _FORWARD)
    with log.open("a") as handle:
        handle.write(f"Traceback (most recent call last):\n{_READ}\n")
    assert _after(pilot_dir, session) == 0

    record = _record(pilot_dir, session)
    assert [(run["kind"], run.get("mode")) for run in record["trace_runs"]] == [
        ("frontend", "daemon")
    ]
    assert record["owner_log"]["tool_calls"] == {"reading": 1, "writing": 0}
    assert record["owner_log"]["non_json_lines"] == 1
    assert record["judgement"] == {
        "mode": "daemon",
        "qualifies": True,
        "not_qualifying_because": [],
        "outcome": "retained",
        "incident_reasons": [],
    }


def test_a_fallback_session_qualifies_on_its_own_read(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)

    def session() -> None:
        _start_frontend(monkeypatch, profile, _ALONE)
        # The frontend drives its own browser, so its own log has the read.
        (run,) = (profile.parent / "trace-runs").iterdir()
        with (run / "server.log").open("a") as handle:
            handle.write(f"{_READ}\n")

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["mode"] == "fallback"
    assert record["judgement"]["qualifies"] is True


def test_a_rewritten_cookie_file_alone_is_not_a_loss(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        log.write_text(f"{_READ}\n")
        # A browser close exports the cookies again under the same login.
        cookies = [
            {"name": "JSESSIONID", "value": "b"},
            {"name": "li_at", "value": "c"},
        ]
        portable_cookie_path(profile).write_text(json.dumps(cookies))

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["outcome"] == "retained"


def test_an_attested_login_never_resolves_a_new_generation(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        log.write_text(f"{_READ}\n")
        write_source_state(profile)

    record = _run_session(
        pilot_dir, candidate, profile, session, "login@2026-10-15T10:00Z"
    )

    verdict = record["judgement"]
    assert verdict["outcome"] == "incident"
    assert "login_generation changed" in verdict["incident_reasons"]
    assert verdict["qualifies"] is False


def test_the_owner_s_expiry_line_does_not_close_an_incident(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    expiry = _log_line(
        "linkedin_mcp_server.daemon.liveness",
        "Nobody has waited for call c-1 in 60s; stopping it",
    )

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        log.write_text(f"{_READ}\n{expiry}\n")
        portable_cookie_path(profile).write_text("[]")

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["outcome"] == "incident"
    assert record["judgement"]["incident_reasons"] == ["the li_at cookie is gone"]


def test_an_owner_log_without_a_tool_call_does_not_qualify(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    idle = _log_line(
        "linkedin_mcp_server.daemon.owner",
        "Nothing has needed the browser in 600s; exiting",
    )

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        log.write_text(f"{idle}\n")

    verdict = _run_session(pilot_dir, candidate, profile, session)["judgement"]

    assert verdict["outcome"] == "retained"
    assert verdict["not_qualifying_because"] == [
        "no LinkedIn-reading tool call was logged"
    ]


def test_a_new_quarantine_is_an_incident(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    quarantine = f"{QUARANTINE_PREFIX}20261015T120000Z"

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        log.write_text(f"{_READ}\n")
        (profile.parent / quarantine).mkdir()

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["incident_reasons"] == [f"new quarantine {quarantine}"]


def test_an_owner_log_cut_short_and_grown_again_is_an_incident(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), ["Traceback (most recent call last):"])

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        # In place: the same file, now longer than it was.
        log.write_text(f"{_READ}\n{_READ}\n")

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["incident_reasons"] == ["the owner log was rewritten"]


@pytest.mark.parametrize("link", ["trace log", "trace runs", "owner log"])
def test_a_linked_log_is_never_copied(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    pilot_dir: Path,
    candidate: _Candidate,
    link: str,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    secret = log.with_name("token-synthetic")
    secret.write_text("synthetic-secret")

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        runs = profile.parent / "trace-runs"
        try:
            if link == "trace log":
                (runs / "run-linked").mkdir()
                (runs / "run-linked" / "server.log").symlink_to(secret)
            elif link == "trace runs":
                (runs / "run-linked").mkdir()
                (runs / "run-linked" / "server.log").write_text("synthetic-secret")
                elsewhere = tmp_path_factory.mktemp("elsewhere") / "trace-runs"
                os.replace(runs, elsewhere)
                runs.symlink_to(elsewhere, target_is_directory=True)
            else:
                log.unlink()
                log.symlink_to(secret)
        except OSError:
            pytest.skip("this account cannot create symlinks")

    record = _run_session(pilot_dir, candidate, profile, session)

    copied = (pilot_dir / record["session"]).rglob("*")
    assert not any(
        b"synthetic-secret" in path.read_bytes() for path in copied if path.is_file()
    )
    if link == "owner log":
        assert "the owner log was refused" in record["judgement"]["incident_reasons"]
    else:
        assert record["judgement"]["mode"] == "uncertain"


def test_a_second_after_refuses_and_keeps_the_first(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    _publish_owner(profile, _dead_pid(), [_READ])
    session = _run_session(pilot_dir, candidate, profile, lambda: None)["session"]
    first = (pilot_dir / session / "after.json").read_bytes()
    candidate.answer(_valid(profile))

    assert _after(pilot_dir, session) != 0
    assert (pilot_dir / session / "after.json").read_bytes() == first


def test_a_replaced_owner_log_makes_an_incident(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [_READ, _READ])

    def session() -> None:
        _start_frontend(monkeypatch, profile, _FORWARD)
        # Written beside it first, so the new file cannot reuse the inode.
        fresh = log.with_name("fresh.log")
        fresh.write_text(f"{_READ}\n{_READ}\n{_READ}\n")
        os.replace(fresh, log)

    record = _run_session(pilot_dir, candidate, profile, session)

    assert record["judgement"]["incident_reasons"] == ["the owner log was replaced"]


def test_an_owner_that_outlives_the_wait_is_an_incident_and_stays_alive(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    monkeypatch.setattr(pilot, "_IDLE_WAIT_SECONDS", 0)
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:

        def session() -> None:
            _start_frontend(monkeypatch, profile, _FORWARD)
            _publish_owner(profile, owner.pid, [_READ])

        record = _run_session(pilot_dir, candidate, profile, session)

        assert owner.poll() is None
        assert (
            "an owner was still alive when the wait ended"
            in (record["judgement"]["incident_reasons"])
        )
        evidence = pilot_dir / record["session"] / record["evidence"]
        assert (evidence / "daemon.log").read_text() == f"{_READ}\n"
    finally:
        owner.kill()
        owner.wait()


# --- Refusals and the auth root ----------------------------------------------


def test_a_live_owner_refuses_the_cold_start(
    isolate_profile_dir: Path, pilot_dir: Path, candidate: _Candidate
) -> None:
    _store(isolate_profile_dir)
    _publish_owner(isolate_profile_dir, os.getpid(), [])

    assert _before(pilot_dir, candidate, isolate_profile_dir) == (2, "")


def test_a_pilot_dir_reaching_into_the_auth_root_is_refused(
    isolate_profile_dir: Path,
    tmp_path_factory: pytest.TempPathFactory,
    candidate: _Candidate,
) -> None:
    inside = isolate_profile_dir.parent / "pilot"
    inside.mkdir()
    link = tmp_path_factory.mktemp("elsewhere") / "pilot"
    try:
        link.symlink_to(inside, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")

    assert _before(link, candidate, isolate_profile_dir) == (2, "")
    assert list(inside.iterdir()) == []


def test_a_pilot_dir_in_the_daemon_state_is_refused(
    isolate_profile_dir: Path, candidate: _Candidate
) -> None:
    _store(isolate_profile_dir)
    candidate.answer(_valid(isolate_profile_dir))
    inside = descriptor.daemon_state_root().parent / "pilot"

    assert _before(inside, candidate, isolate_profile_dir) == (2, "")
    assert not inside.exists()


@pytest.mark.parametrize(
    ("retry", "mode", "outcome"),
    [
        ("as is", "daemon", "retained"),
        # The first capture saw the loss; the status that followed hid it.
        ("status repaired a loss", "daemon", "incident"),
        ("source trace vanished", "uncertain", "retained"),
        ("captured copy lost", "uncertain", "incident"),
        # Edited between the attempts: the quiet frontend's copy stays.
        ("frontend dropped from the manifest", "uncertain", "incident"),
        ("manifest emptied", "uncertain", "incident"),
    ],
)
def test_a_retried_after_judges_its_first_capture(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
    retry: str,
    mode: str,
    outcome: str,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    candidate.answer(_valid(profile), _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0
    runs = profile.parent / "trace-runs"
    _start_frontend(monkeypatch, profile, _FORWARD)
    log.write_text(f"{_READ}\n")
    known = set(pilot.trace_runs(profile.parent))
    if retry not in ("as is", "status repaired a loss"):
        _start_frontend(
            monkeypatch, profile, _FORWARD, log_level="WARNING", transport_fails=True
        )
    quiet = sorted(set(pilot.trace_runs(profile.parent)) - known)
    cookies = portable_cookie_path(profile)
    kept = cookies.read_bytes()
    if retry == "status repaired a loss":
        cookies.write_text("[]")

    def interrupted(*_args: Any) -> None:
        # Ctrl-C in the final status, which has started a trace run of its
        # own and, closing the browser, exported the cookies again.
        (runs / "run-interrupted-status").mkdir()
        (runs / "run-interrupted-status" / "server.log").write_text("")
        cookies.write_bytes(kept)
        raise KeyboardInterrupt

    with (
        patch.object(pilot, "run_status", interrupted),
        pytest.raises(KeyboardInterrupt),
    ):
        _after(pilot_dir, session)
    if retry == "source trace vanished":
        shutil.rmtree(runs / quiet[0])
    elif retry == "captured copy lost":
        (
            pilot_dir / session / "capture" / "trace-runs" / quiet[0] / "server.log"
        ).unlink()
    elif retry == "frontend dropped from the manifest":
        manifest = pilot_dir / session / "capture" / "capture.json"
        captured = json.loads(manifest.read_text())
        captured["trace_runs"] = [
            run for run in captured["trace_runs"] if run["name"] != quiet[0]
        ]
        manifest.write_text(json.dumps(captured))
    elif retry == "manifest emptied":
        (pilot_dir / session / "capture" / "capture.json").write_text("{}")

    assert _after(pilot_dir, session) == 0
    verdict = _record(pilot_dir, session)["judgement"]
    assert (verdict["mode"], verdict["outcome"]) == (mode, outcome)


_ODD_FIELDS: dict[str, tuple[tuple[str | int, ...], Any]] = {
    "top null": ((), None),
    "top array": ((), []),
    "wait null": (("owner_wait",), None),
    "wait timeout array": (("owner_wait", "timed_out"), []),
    "snapshot null": (("snapshot",), None),
    "cookie store null": (("snapshot", "cookies"), None),
    "source store null": (("snapshot", "source_state"), None),
    "quarantine names null": (("snapshot", "quarantines", "names"), None),
    "quarantine names number": (("snapshot", "quarantines", "names"), 7),
    "quarantine name object": (("snapshot", "quarantines", "names"), [{}]),
    "runs null": (("trace_runs",), None),
    "run null": (("trace_runs", 0), None),
    "run kind array": (("trace_runs", 0, "kind"), []),
    "run kind object": (("trace_runs", 0, "kind"), {}),
    "run mode array": (("trace_runs", 0, "mode"), []),
    "run mode object": (("trace_runs", 0, "mode"), {}),
    "run command array": (("trace_runs", 0, "command"), []),
    "run name array": (("trace_runs", 0, "name"), []),
    "run evidence array": (("trace_runs", 0, "evidence"), []),
    # The right shape, but not where the copy of that run is written.
    "run evidence elsewhere": (
        ("trace_runs", 0, "evidence"),
        "trace-runs/run-other/server.log",
    ),
    "owner log counts null": (("owner_log", "tool_calls"), None),
    "owner log reading bool": (("owner_log", "tool_calls", "reading"), True),
    # Keys capture never writes, aimed at fields after sets itself.
    "attestations null": (("attestations",), None),
    "attestations emptied": (("attestations",), []),
    "unknown nested key": (("snapshot", "cookies", "li_at_restored"), True),
}
_ODD_BYTES = {
    "not utf-8": b"\xff\xfe\x80",
    "cut-off json": b'{"owner_wait":',
    "deeply nested json": b"[" * 1200 + b"null" + b"]" * 1200,
}


@pytest.mark.parametrize("odd", [*_ODD_FIELDS, *_ODD_BYTES])
def test_an_odd_capture_is_an_incident_and_never_raises(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
    odd: str,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    candidate.answer(_valid(profile), _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0
    _start_frontend(monkeypatch, profile, _FORWARD)
    log.write_text(f"{_READ}\n")
    with (
        patch.object(pilot, "run_status", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        _after(pilot_dir, session)
    manifest = pilot_dir / session / "capture" / "capture.json"
    if odd in _ODD_BYTES:
        manifest.write_bytes(_ODD_BYTES[odd])
    else:
        path, value = _ODD_FIELDS[odd]
        captured: Any = json.loads(manifest.read_text())
        if path:
            parent = captured
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = value
        else:
            captured = value
        manifest.write_text(json.dumps(captured))

    assert _after(pilot_dir, session, "login@2026-10-15T10:00Z") == 0
    record = _record(pilot_dir, session)
    assert record["attestations"] == ["login@2026-10-15T10:00Z"]
    assert (record["judgement"]["mode"], record["judgement"]["outcome"]) == (
        "uncertain",
        "incident",
    )


def test_a_capture_cut_short_is_an_incident(
    monkeypatch: pytest.MonkeyPatch,
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    log = _publish_owner(profile, _dead_pid(), [])
    candidate.answer(_valid(profile), _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0
    _start_frontend(monkeypatch, profile, _FORWARD)
    log.write_text(f"{_READ}\n")

    with (
        patch.object(pilot, "copy_trace_runs", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        _after(pilot_dir, session)

    assert _after(pilot_dir, session) == 0
    verdict = _record(pilot_dir, session)["judgement"]
    assert (verdict["mode"], verdict["outcome"]) == ("uncertain", "incident")


@pytest.mark.parametrize("link", ["session folder", "evidence file", "capture"])
def test_after_never_writes_through_a_link(
    isolate_profile_dir: Path,
    pilot_dir: Path,
    candidate: _Candidate,
    link: str,
) -> None:
    profile = isolate_profile_dir
    _store(profile)
    _publish_owner(profile, _dead_pid(), [_READ])
    candidate.answer(_valid(profile), _valid(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 0
    folder = pilot_dir / session
    try:
        if link == "session folder":
            moved = profile.parent / "moved-session"
            os.replace(folder, moved)
            folder.symlink_to(moved, target_is_directory=True)
        elif link == "evidence file":
            (folder / "daemon.log").symlink_to(portable_cookie_path(profile))
        else:
            target = profile.parent / "capture"
            target.mkdir()
            (folder / "capture").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    stores = _tree(profile.parent)

    assert _after(pilot_dir, session) == 2
    assert _tree(profile.parent) == stores


def _tree(root: Path) -> list[tuple[str, int, int]]:
    if not root.exists():
        return []
    return sorted(
        (str(path.relative_to(root)), path.stat().st_size, path.stat().st_mtime_ns)
        for path in [root, *root.rglob("*")]
    )


@pytest.mark.parametrize("root", ["absent", "foreign"])
def test_the_recorder_writes_nothing_under_the_auth_root(
    tmp_path_factory: pytest.TempPathFactory,
    pilot_dir: Path,
    candidate: _Candidate,
    root: str,
) -> None:
    auth_root = tmp_path_factory.mktemp("roots") / root
    if root == "foreign":
        auth_root.mkdir()
        (auth_root / "notes.txt").write_text("not ours")
    profile = auth_root / "profile"
    home = descriptor.daemon_state_root().parent.parent
    watched = (auth_root, home)
    trees = [_tree(path) for path in watched]

    candidate.answer(_no_session(profile), _no_session(profile))
    code, session = _before(pilot_dir, candidate, profile)
    assert code == 1
    assert _after(pilot_dir, session) == 0

    assert _record(pilot_dir, session)["judgement"]["outcome"] == "not_counted"
    assert [_tree(path) for path in watched] == trees
    assert auth_root.exists() == (root == "foreign")


# --- tally -------------------------------------------------------------------


_SHA = "a7e8044bf92fe4588acee93ac1c15b696bd14337"


def _tally(*folders: Path) -> int:
    return pilot.main(["tally", "--sha", _SHA, *map(str, folders)])


def _recorded(
    folder: Path,
    session: str,
    os_name: str,
    sha: str = _SHA,
    counts: bool = True,
    **judgement: Any,
) -> None:
    (folder / session).mkdir(parents=True)
    before = {"session": session, "os": os_name, "candidate_sha": sha, "counts": counts}
    (folder / session / "before.json").write_text(json.dumps(before))
    if judgement:
        verdict = {
            "mode": "daemon",
            "qualifies": True,
            "outcome": "retained",
            "incident_reasons": [],
            **judgement,
        }
        (folder / session / "after.json").write_text(json.dumps({"judgement": verdict}))


def test_tally_passes_only_on_met_quotas_without_incidents_or_doubt(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    monkeypatch.setattr(pilot, "QUOTAS", {"macos": 2, "linux": 1})
    first, second = tmp_path_factory.mktemp("a"), tmp_path_factory.mktemp("b")
    _recorded(first, "s1", "macos", mode="daemon")
    _recorded(second, "s1", "macos", mode="daemon")  # the same session again
    _recorded(first, "s2", "linux", mode="daemon")
    _recorded(first, "s3", "linux", mode="fallback")
    _recorded(first, "s0", "macos", sha="0" * 40, mode="daemon")  # another build
    # A start that did not count, left without `after` as the guide says.
    _recorded(first, "s6", "linux", counts=False)
    assert _tally(first, second) == 1

    _recorded(second, "s4", "macos", mode="daemon")
    assert _tally(first, second) == 0

    # Uncertain without a read: does not qualify, and still blocks.
    _recorded(second, "s5", "linux", mode="uncertain", qualifies=False)
    assert _tally(first, second) == 1


@pytest.mark.parametrize(
    ("candidate", "sha"),
    [
        (
            f"uvx --isolated --cache-dir C --from git+https://github.com/o/r@{_SHA.upper()} mcp-server-linkedin",
            _SHA,
        ),
        ("uvx --from /Users/me/checkout mcp-server-linkedin", None),
        (f"uvx --from git+https://github.com/o/r@{_SHA}0 mcp-server-linkedin", None),
    ],
)
def test_a_candidate_is_known_by_its_full_commit(
    candidate: str, sha: str | None
) -> None:
    assert pilot.candidate_sha(candidate) == sha


@pytest.mark.parametrize(
    "judgement",
    [
        {"outcome": "incident", "qualifies": False, "incident_reasons": ["x"]},
        {},  # after was never recorded
    ],
)
def test_tally_fails_on_an_open_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
    judgement: dict[str, Any],
) -> None:
    monkeypatch.setattr(pilot, "QUOTAS", {"macos": 1})
    folder = tmp_path_factory.mktemp("pilot")
    _recorded(folder, "s1", "macos", mode="daemon")
    _recorded(folder, "s2", "macos", **judgement)

    assert _tally(folder) == 1
