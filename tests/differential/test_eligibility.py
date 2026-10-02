"""Refused configurations and a rival owner: the product's own lines, the
classification of planted metadata, the planting itself, the HTTP host,
the server lifetimes, the verdicts, the rows' wiring and the model mapping.

No browser, no real provider configuration, no real home. The **lines** are
the product's own log records from its real decisions, formatted by its own
JSON formatter. The **classification** is the product's real classifier
reading metadata planted in a temporary home. The **planting** writes and
removes under ``tmp_path`` only. The **HTTP host** is ``run_http_host_session``
against a stand-in FastMCP server of its own process on a loopback port
(POSIX). The **verdicts** start from an explicit valid record of each cell
and change one observation at a time, the plan's controls among them. The
**wiring** runs the real row entry on the preservation gate's modelled
actors, with a host double whose reads are real requests to a real origin
and a watcher double whose records name which server launched which
browser; H-R14's daemon script runs on a modelled context. The **model
mapping** names tests that exist.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import json
import logging
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from differential import eligibility_rows, harness, lease_probe, unconfirmed_close
from differential.call_loss import EXPECTED_SECTIONS, INVALID, PERSON_TOOL
from differential.eligibility_rows import (
    A2_USERNAME,
    ASK_AGAIN_LINE,
    B_USERNAME,
    CASES,
    CLOUD_STORAGE,
    CONTAINER_LINE,
    DROPBOX_AUTH_ROOT,
    FORWARDING_LINE,
    INELIGIBLE_IDLE_TIMEOUT_SECONDS,
    K2_NOT_APPLICABLE,
    LIVE_RIVAL_LINE,
    MODEL_COVERAGE,
    NO_OWNER_LINE,
    RIVAL_ENVIRONMENT,
    NEAR_IDLE,
    RIVAL_IDLE_MARGIN_SECONDS,
    RIVAL_IDLE_TIMEOUT_SECONDS,
    RIVAL_OVERRIDES,
    ROW_CONTAINER,
    ROW_DISABLED_ENV,
    ROW_HTTP,
    ROW_NO_DAEMON,
    ROW_RIVAL,
    ROW_STATE_SYNCED,
    ROW_SYNCED,
    ROW_UNKNOWN,
    SILENT_RIVAL_LINE,
    STREAMABLE_HTTP,
    Planted,
    ProviderConfigExists,
    cloud_storage_profile,
    cloud_storage_root,
    comparison_refusals,
    decision_lines,
    ineligible_problems,
    invalid_evidence,
    owner_lines,
    plant_file,
    plant_for,
    plant_tree,
    problems_for,
    rival_problems,
    rival_script,
    semantic_differences,
    server_members,
    storage_reading,
)
from differential.events import EventLog
from differential.harness import (
    MUST_NOT_REPAIR,
    ORDINARY,
    STDIO,
    CoordinationSeams,
    DaemonCleanup,
    RowContext,
    RowLifecycle,
    browser_lineage,
    frontend_lifetimes,
    host_failures,
    launch_lifetimes,
    lifecycle_problems,
    measure_host_quit_row,
    process_environment,
    run_http_host_session,
)
from differential.lease_probe import FREE
from differential.synthetic_origin import person_path
from differential.test_call_loss import (  # noqa: F401 - fixtures
    _CalibrationScene,
    certificates,
    origin,
)
from differential.test_preservation_gate import (  # noqa: F401 - fixtures
    _SETTLED,
    _Watcher,
    profile,
)
from differential.test_profile_commands import _functions
from differential.watcher import OWNER_MODULE
from linkedin_mcp_server.config.loaders import EnvironmentKeys
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.logging_config import MCPJSONFormatter

MS = 1_000_000
NOW = 1_800_000_000.0
PID_A = 4242
PID_B = 5151
OWNER = (6161, NOW - 50.0, "owner-instance")


@pytest.fixture(autouse=True)
def owned(monkeypatch):
    """Fresh registries, so one test's retained worker never gates the next."""
    monkeypatch.setattr(unconfirmed_close, "_OWNED", [])
    monkeypatch.setattr(unconfirmed_close, "_RETAINED", [])
    monkeypatch.setattr(lease_probe, "_OWNED", [])


# --- The product's own lines --------------------------------------------------------


def _formatted(records: list[logging.LogRecord]) -> list[str]:
    """The records as the server writes them to stderr when not interactive."""
    formatter = MCPJSONFormatter()
    return [formatter.format(record) for record in records]


def _eligible_config(profile_dir: Path) -> AppConfig:
    config = AppConfig()
    config.server.daemon_enabled = True
    config.browser.user_data_dir = str(profile_dir)
    return config


def _temporary_home(monkeypatch, home: Path) -> Path:
    """The product's provider lookups pointed at *home* alone: its Dropbox
    file and its macOS provider folders. Nothing outside it is read for a
    provider."""
    from linkedin_mcp_server import storage_class

    home.mkdir(parents=True, exist_ok=True)
    info = home / ".dropbox" / "info.json"
    monkeypatch.setattr(storage_class, "_homes", lambda: [home])
    monkeypatch.setattr(
        storage_class, "_dropbox_info_files", lambda _platform, _homes: [info]
    )
    monkeypatch.setattr(storage_class, "_onedrive_registered_folders", lambda: [])
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        monkeypatch.delenv(name, raising=False)
    return info


_STORAGE_CELLS = [
    pytest.param(row, id=row)
    for row in (ROW_SYNCED, ROW_STATE_SYNCED, ROW_UNKNOWN, eligibility_rows.ROW_CLOUD)
]


def _plant_in(row: str, home: Path, profile_dir: Path) -> tuple[Planted, Path]:
    """The cell's metadata planted in *home*, and the profile the cell runs
    on, the way ``test_eligibility_rows.planted`` plants it on a runner."""
    from linkedin_mcp_server import daemon_descriptor
    from linkedin_mcp_server.session_state import canonical

    case = CASES[row]
    if case.plant == CLOUD_STORAGE:
        root = cloud_storage_root(home, "cell")
        planted = Planted(CLOUD_STORAGE, (plant_tree(root),))
        return planted, cloud_storage_profile(root)
    assert case.plant is not None
    planted = plant_for(
        case.plant,
        auth_root=canonical(profile_dir).parent,
        state_root=daemon_descriptor.daemon_state_root(),
    )
    return planted, profile_dir


@pytest.mark.parametrize("row", _STORAGE_CELLS)
def test_the_planted_metadata_is_classified_and_refused_as_the_cell_declares(
    row, tmp_path, monkeypatch, caplog
):
    """Through the product's real classifier and its real refusal, from a
    temporary home: what the native cell expects is what the product does."""
    case = CASES[row]
    reason = case.skip_reason(sys.platform)
    if reason is not None:
        pytest.skip(reason)
    _temporary_home(monkeypatch, tmp_path / "home")
    planted, profile_dir = _plant_in(row, tmp_path / "home", tmp_path / "auth" / "p")
    try:
        storage = storage_reading(profile_dir)
        from linkedin_mcp_server.daemon import daemon_would_be_used

        with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.daemon"):
            assert daemon_would_be_used(_eligible_config(profile_dir)) is False
    finally:
        assert planted.remove() == []
    assert [storage[name]["class"] for name in storage] == list(case.storage)
    lines = decision_lines(_formatted(caplog.records))
    assert case.refusal is not None
    assert lines["storage"] == [list(case.refusal[1:])]
    assert lines["container"] == 0


def test_a_container_is_refused_in_the_line_the_cell_expects(monkeypatch, caplog):
    from linkedin_mcp_server.daemon import daemon_would_be_used

    monkeypatch.setattr(
        "linkedin_mcp_server.daemon.get_runtime_id", lambda: "linux-x64-container"
    )
    with caplog.at_level(logging.WARNING, logger="linkedin_mcp_server.daemon"):
        assert daemon_would_be_used(_eligible_config(Path("/unused/p"))) is False

    lines = decision_lines(_formatted(caplog.records))
    assert (lines["container"], lines["storage"]) == (1, [])
    assert CASES[ROW_CONTAINER].refusal == ("container",)


@pytest.mark.parametrize(
    ("reach", "line", "kind"),
    [
        ("ANSWERED", LIVE_RIVAL_LINE, "live_rival"),
        ("SILENT", SILENT_RIVAL_LINE, "silent_rival"),
    ],
)
def test_a_rival_left_alone_says_so_in_the_line_counted_as_its_fallback(
    reach, line, kind, caplog
):
    """The election's own decision on a same-build owner of another
    configuration, as it logs it."""
    from linkedin_mcp_server import __version__
    from linkedin_mcp_server.daemon import Attachment, Mismatch, OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon_election import Reach, _decide_control_only

    attachment = Attachment(
        descriptor=SimpleNamespace(  # ty: ignore[invalid-argument-type]
            instance_id="rival", package_version=__version__
        ),
        token="t",
        control_only=True,
    )
    with caplog.at_level(logging.INFO, logger="linkedin_mcp_server.daemon_election"):
        found, asked, _ = _decide_control_only(
            OwnerLookup(state=OwnerState.INCOMPATIBLE, mismatch=Mismatch.CONFIGURATION),
            attachment,
            lambda _attachment, _timeout: getattr(Reach, reach),
            set(),
            may_ask_for_turnover=True,
            deadline=time.monotonic() + 5.0,
        )

    assert found.fallback is not None and not asked
    lines = decision_lines(_formatted(caplog.records))
    assert lines[kind] == 1
    assert lines["election"]["fallback"] == 1
    assert sum(lines[name] for name in ("live_rival", "silent_rival")) == 1
    assert any(line in text for text in _formatted(caplog.records))


def test_the_frontend_says_it_drives_its_own_browser_and_forwards_in_its_own_lines(
    monkeypatch, caplog, tmp_path
):
    """``_obtain_shared_owner``'s two ends, as the frontend logs them."""
    from linkedin_mcp_server import cli_main
    from linkedin_mcp_server.daemon import DirectFallback, OwnerLookup, OwnerState
    from linkedin_mcp_server.daemon_election import ElectionOutcome

    monkeypatch.setattr(
        "linkedin_mcp_server.daemon.daemon_would_be_used", lambda _config: True
    )
    monkeypatch.setattr(
        "linkedin_mcp_server.cli_main.get_profile_dir", lambda: tmp_path / "p"
    )
    fallback = ElectionOutcome(
        OwnerLookup(state=OwnerState.INCOMPATIBLE, fallback=DirectFallback.LIVE_RIVAL)
    )
    attachment = MagicMock(name="attachment")
    attachment.control_only = False
    forwarded = ElectionOutcome(
        OwnerLookup(state=OwnerState.ATTACHABLE, attachment=attachment)
    )
    outcomes = iter([fallback, forwarded])
    monkeypatch.setattr(
        "linkedin_mcp_server.daemon_election.obtain_owner",
        lambda *_args, **_kwargs: next(outcomes),
    )
    with caplog.at_level(logging.INFO):
        assert cli_main._obtain_shared_owner(AppConfig()) is None
        fell_back = _formatted(caplog.records)
        caplog.clear()
        assert cli_main._obtain_shared_owner(AppConfig()) is not None
        went_on = _formatted(caplog.records)

    assert decision_lines(fell_back)["election"]["no_owner"] == 1
    assert decision_lines(fell_back)["forwarding"] == 0
    assert decision_lines(went_on)["forwarding"] == 1


def test_a_custom_browser_s_line_is_no_storage_refusal():
    # The CHROME_PATH line shares the storage line's tail; only the storage
    # line names a root and a class.
    lines = decision_lines(
        [
            "CHROME_PATH is set, so this server drives its own browser instead "
            "of sharing one"
        ]
    )
    assert lines["storage"] == [] and lines["container"] == 0


def test_the_owner_s_lines_are_counted_by_what_they_say():
    assert owner_lines(
        [
            "Nothing has needed the browser in 120s; shutting down",
            "A newer build asked for the browser; standing down",
            "A profile command asked for the browser; standing down",
            "Standing down: wedged",
        ]
    ) == {"idle_exit": 1, "stood_down": 3}


# --- Planting and removing ----------------------------------------------------------


def test_a_provider_configuration_already_there_refuses_the_plant_and_is_untouched(
    tmp_path,
):
    there = tmp_path / "home" / ".dropbox" / "info.json"
    there.parent.mkdir(parents=True)
    there.write_bytes(b'{"business": {"path": "/x"}}')
    other = tmp_path / "other" / "Dropbox" / "info.json"

    for target in (there, other):
        with pytest.raises(ProviderConfigExists):
            plant_file(target, b"{}", locations=[other, there])

    assert there.read_bytes() == b'{"business": {"path": "/x"}}'
    assert not other.parent.exists()


def test_a_dangling_link_where_the_file_would_be_refuses_the_plant(tmp_path):
    if sys.platform == "win32":
        pytest.skip("creating a symlink needs a privilege a runner may not hold")
    target = tmp_path / ".dropbox" / "info.json"
    target.parent.mkdir()
    target.symlink_to(tmp_path / "nowhere")

    with pytest.raises(ProviderConfigExists):
        plant_file(target, b"{}", locations=[target])


def test_a_plant_is_removed_exactly_with_only_the_directories_it_made(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "kept.txt").write_text("the home's own")
    target = home / ".dropbox" / "nested" / "info.json"

    planted = plant_file(target, b'{"personal": {"path": "/x"}}', locations=[target])

    assert target.read_bytes() == b'{"personal": {"path": "/x"}}'
    assert planted.created == (home / ".dropbox", home / ".dropbox" / "nested")
    assert planted.remove() == []
    assert sorted(p.name for p in home.iterdir()) == ["kept.txt"]
    assert planted.remove() == [], "a second removal touches nothing"


def test_a_plant_that_changed_is_left_in_place_and_reported(tmp_path):
    target = tmp_path / ".dropbox" / "info.json"
    planted = plant_file(target, b"{}", locations=[target])
    target.write_bytes(b'{"personal": {"path": "/somebody"}}')

    problems = planted.remove()

    assert problems and "left as they are" in problems[0]
    assert target.read_bytes() == b'{"personal": {"path": "/somebody"}}'


def test_a_plant_replaced_by_the_same_bytes_is_left_in_place_and_reported(tmp_path):
    target = tmp_path / ".dropbox" / "info.json"
    planted = plant_file(target, b"{}", locations=[target])
    replacement = tmp_path / ".dropbox" / "info.json.new"
    replacement.write_bytes(b"{}")
    os.replace(replacement, target)

    problems = planted.remove()

    assert problems and "replaced by another file" in problems[0]
    assert target.read_bytes() == b"{}"


def test_a_plant_outside_the_product_s_locations_is_refused(tmp_path):
    with pytest.raises(ValueError):
        plant_file(tmp_path / "info.json", b"{}", locations=[tmp_path / "x.json"])
    assert not (tmp_path / "info.json").exists()


def test_the_cloud_cell_s_auth_root_is_left_for_the_product_to_claim(tmp_path):
    """Measured on macOS CI: a profile whose auth root the cell had created
    was refused as unclaimed before anything ran."""
    from linkedin_mcp_server.profile_claim import ensure_profile_claim

    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    root = cloud_storage_root(home, "cell")
    planted = plant_tree(root)
    try:
        profile = cloud_storage_profile(root)
        assert not profile.parent.exists()
        ensure_profile_claim(profile)
        assert profile.parent.is_dir()
    finally:
        import shutil

        shutil.rmtree(root / "auth", ignore_errors=True)
        assert planted.remove() == []


def test_a_planted_tree_refuses_an_existing_root_and_is_removed_exactly(tmp_path):
    home = tmp_path / "home"
    (home / "Library").mkdir(parents=True)
    root = cloud_storage_root(home, "cell")

    planted = plant_tree(root)
    (root / "profile").mkdir()
    (root / "profile" / "Cookies").write_bytes(b"synthetic")
    with pytest.raises(ProviderConfigExists):
        plant_tree(root)

    assert planted.remove() == []
    assert not (home / "Library" / "CloudStorage").exists()
    assert (home / "Library").is_dir(), "an ancestor the plant did not make stays"


def test_a_dropbox_plant_names_the_auth_root_the_state_root_or_nothing(
    tmp_path, monkeypatch
):
    info = _temporary_home(monkeypatch, tmp_path / "home")
    for kind, wanted in (
        (DROPBOX_AUTH_ROOT, str(tmp_path / "auth")),
        (eligibility_rows.DROPBOX_STATE_ROOT, str(tmp_path / "state")),
    ):
        planted = plant_for(
            kind, auth_root=tmp_path / "auth", state_root=tmp_path / "state"
        )
        assert json.loads(info.read_bytes())["personal"]["path"] == wanted
        assert planted.remove() == []
    planted = plant_for(
        eligibility_rows.DROPBOX_MALFORMED,
        auth_root=tmp_path / "auth",
        state_root=tmp_path / "state",
    )
    with pytest.raises(ValueError):
        json.loads(info.read_bytes())
    assert planted.remove() == []
    assert not info.parent.exists()


# --- The HTTP host, against a stand-in server -----------------------------------------

_STAND_IN_HTTP_SERVER = """
import argparse
import signal
import sys
import threading
import time

from fastmcp import FastMCP

parser = argparse.ArgumentParser()
parser.add_argument("ending")
parser.add_argument("--transport")
parser.add_argument("--host")
parser.add_argument("--port", type=int)
parser.add_argument("--path")
options = parser.parse_args()
if options.ending == "early":
    print("stand-in server refusing to start", file=sys.stderr, flush=True)
    sys.exit(3)
mcp = FastMCP("stand-in")


@mcp.tool
def get_feed(num_posts: int = 1) -> dict:
    return {"sections": {"feed": "the stand-in's post"}}


def serve():
    mcp.run(
        transport="http",
        host=options.host,
        port=options.port,
        path=options.path,
        show_banner=False,
    )


print("stand-in http server up", file=sys.stderr, flush=True)
if options.ending == "deaf":
    # Off the main thread uvicorn installs no handler, so the interrupt is
    # ignored and nothing ends the server but a kill.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    threading.Thread(target=serve, daemon=True).start()
    while True:
        time.sleep(1)
try:
    serve()
except KeyboardInterrupt:
    pass
print("stand-in http server closed", file=sys.stderr, flush=True)
"""

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason=eligibility_rows.HTTP_NOT_ON_WINDOWS
)


async def _http_session(tmp_path: Path, ending: str, **kwargs: Any):
    server = tmp_path / "stand_in_http.py"
    server.write_text(_STAND_IN_HTTP_SERVER)
    lines: list[str] = []
    session = await run_http_host_session(
        [sys.executable, str(server), ending],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=lines.append,
        **kwargs,
    )
    return session, lines


@posix_only
async def test_an_http_host_initializes_calls_and_quits_by_interrupt(tmp_path):
    started: list[int] = []
    session, lines = await _http_session(tmp_path, "normal", started=started.append)

    assert session.error is None, session.error
    assert session.transport == STREAMABLE_HTTP
    assert session.tool is not None and session.tool["is_error"] is False
    assert started == [session.pid]
    assert host_failures(session) == []
    assert session.interrupted is True and session.exit_code == 0
    assert session.interrupt_monotonic_ns is not None
    assert session.exit_seen_monotonic_ns is not None
    assert session.exit_seen_monotonic_ns >= session.interrupt_monotonic_ns
    assert "stand-in http server up" in lines
    assert "stand-in http server closed" in lines
    summary = harness.host_summary(session)
    assert eligibility_rows.http_host_problems(summary) == []


@posix_only
async def test_an_http_server_that_ignores_its_interrupt_is_killed_and_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(harness, "_HOST_EXIT_SECONDS", 1.0)
    session, _ = await _http_session(tmp_path, "deaf")

    assert session.killed_by_harness is True
    assert "the harness had to kill the server" in host_failures(session)
    assert eligibility_rows.http_host_problems(harness.host_summary(session))


@posix_only
async def test_an_http_server_whose_registration_fails_is_not_left_running(
    tmp_path,
):
    spawned: list[Any] = []

    def register(process: Any) -> None:
        spawned.append(process)
        raise RuntimeError("registration failed")

    try:
        with contextlib.suppress(RuntimeError):
            session, _ = await _http_session(tmp_path, "normal", on_process=register)
            assert session.error is not None
        [process] = spawned
        assert process.returncode is not None
    finally:
        for process in spawned:
            if process.returncode is None:
                process.kill()
                await process.wait()


@posix_only
async def test_an_http_server_that_never_listens_fails_its_host(tmp_path):
    session, lines = await _http_session(tmp_path, "early")

    assert session.error is not None and "before it listened" in session.error
    assert host_failures(session) == [f"the host session failed: {session.error}"]
    assert "stand-in server refusing to start" in lines


@posix_only
async def test_an_http_host_refuses_what_it_does_not_run(tmp_path):
    with pytest.raises(ValueError, match="second_call"):
        await run_http_host_session(
            ["unused"],
            env={},
            cwd=tmp_path,
            on_stderr=lambda _line: None,
            second_call=True,
        )


async def test_an_http_host_refuses_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(harness.HttpHostRefused):
        await run_http_host_session(
            ["unused"], env={}, cwd=tmp_path, on_stderr=lambda _line: None
        )


def test_a_stdio_host_s_failures_read_as_before():
    session = harness.HostSession(
        alive_before_quit=True, stdin_closed=False, exited_on_quit=True, exit_code=1
    )
    assert host_failures(session) == [
        "closing the server's stdin failed: None",
        "the server exited abnormally, with status 1, after stdin EOF",
    ]


def test_an_http_host_s_failures_name_its_interrupt():
    session = harness.HostSession(
        transport=STREAMABLE_HTTP,
        alive_before_quit=True,
        interrupted=False,
        interrupt_error="ProcessLookupError: gone",
        exited_on_quit=False,
    )
    assert host_failures(session) == [
        "interrupting the HTTP server failed: ProcessLookupError: gone",
        f"the server did not exit within {harness._HOST_EXIT_SECONDS}s of its "
        f"interrupt",
    ]


def test_the_row_s_runtime_settings_are_set_for_the_block_and_then_undone(
    monkeypatch,
):
    monkeypatch.delenv("LINKEDIN_MCP_CONTAINER", raising=False)
    monkeypatch.setenv("LINKEDIN_MCP_KEPT", "before")
    with process_environment(
        {"LINKEDIN_MCP_CONTAINER": "true", "LINKEDIN_MCP_KEPT": "during"}
    ):
        assert os.environ["LINKEDIN_MCP_CONTAINER"] == "true"
        assert os.environ["LINKEDIN_MCP_KEPT"] == "during"
    assert "LINKEDIN_MCP_CONTAINER" not in os.environ
    assert os.environ["LINKEDIN_MCP_KEPT"] == "before"


# --- Server lifetimes and the browsers they launched -----------------------------------


def _start(pid, start, ppid, actor, cmdline, *, t=None):
    return {
        "kind": "process.start",
        "pid": pid,
        "start_identity": start,
        "ppid": ppid,
        "actor": actor,
        "in_row": True,
        "cmdline": cmdline,
        "t": start if t is None else t,
    }


_SERVER = [sys.executable, "-m", "linkedin_mcp_server"]
_OWNER_CMD = [sys.executable, "-m", OWNER_MODULE]


def test_a_server_s_lifetime_ties_the_browser_it_launched_to_its_host():
    records = [
        _start(PID_A, NOW, 1, "frontend", _SERVER),
        _start(6161, NOW + 0.5, PID_A, "owner", _OWNER_CMD),
        _start(7000, NOW + 1.0, PID_A, "driver", ["node", "cli.js"]),
        _start(7001, NOW + 1.2, 7000, "browser", ["chrome", "--user-data-dir=/p"]),
    ]

    frontends = frontend_lifetimes(records)
    owners, gates = launch_lifetimes(records, [])

    assert [p[:3] for p in frontends] == [[PID_A, NOW, 1]]
    assert [p[:3] for p in owners] == [[6161, NOW + 0.5, PID_A]], "unchanged"
    assert gates == []
    record = {
        "frontend_processes": frontends,
        "browser_roots": browser_lineage(records),
    }
    members = server_members(record, PID_A)
    assert [m[:2] for m in members] == [[PID_A, NOW]]
    assert eligibility_rows._browsers_of(members, record) == [
        [7001, NOW + 1.2, None, PID_A, NOW]
    ]


def test_a_windows_launcher_s_interpreter_is_the_same_server():
    # The venv launcher starts the interpreter with the same command line and
    # is still read after the child first was; the interpreter drives the
    # browser.
    launcher = [PID_A, NOW, 1, "digest", None, NOW + 0.1, NOW + 5.0]
    child = [PID_A + 1, NOW + 0.05, PID_A, "digest", None, NOW + 0.1, NOW + 5.0]
    other = [PID_A + 2, NOW + 0.05, PID_A, "other", None, NOW + 0.1, NOW + 5.0]
    record = {"frontend_processes": [launcher, child, other]}

    assert server_members(record, PID_A) == [launcher, child]
    assert server_members(record, PID_A, launched=NOW + 2.0) == []
    assert server_members(record, None) == []


# --- Verdicts: records ----------------------------------------------------------------


def _call(tool: str, began: float, ended: float, **fields: Any) -> dict[str, Any]:
    return {
        "tool": tool,
        "began": began,
        "ended": ended,
        "began_monotonic_ns": int((began - NOW) * 1000) * MS,
        "ended_monotonic_ns": int((ended - NOW) * 1000) * MS,
        "outcome": "returned",
        "is_error": False,
        **fields,
    }


def _feed_call(began: float, ended: float) -> dict[str, Any]:
    return _call("get_feed", began, ended, read_the_post=True)


def _person_call(began: float, ended: float) -> dict[str, Any]:
    return _call(
        PERSON_TOOL,
        began,
        ended,
        read_the_post=False,
        marked_sections=list(EXPECTED_SECTIONS),
        section_errors=[],
    )


def _request(path: str, t: float) -> dict[str, Any]:
    return {
        "host": "www.linkedin.com",
        "path": path,
        "t": t,
        "session_valid": True,
        "monotonic_ns": int((t - NOW) * 1000) * MS,
    }


def _pages(username: str, t: float) -> list[dict[str, Any]]:
    return [
        _request(person_path(username, section), t + i * 0.2)
        for i, section in enumerate(EXPECTED_SECTIONS)
    ]


def _lifetime(pid: int, start: float, ppid: int = 1) -> list[Any]:
    return [pid, start, ppid, f"digest-{pid}", None, start + 0.1, NOW + 100.0]


def _stdio_host() -> dict[str, Any]:
    return {
        "error": None,
        "alive_before_quit": True,
        "stdin_closed": True,
        "exited_on_quit": True,
        "exit_code": 0,
        "killed_by_harness": False,
        "eof_ns": 60_000 * MS,
        "exit_seen_ns": 61_000 * MS,
        "transport": STDIO,
    }


def _http_host() -> dict[str, Any]:
    return {
        "error": None,
        "alive_before_quit": True,
        "interrupted": True,
        "interrupt_error": None,
        "exited_on_quit": True,
        "exit_code": 0,
        "killed_by_harness": False,
        "interrupt_ns": 60_000 * MS,
        "exit_seen_ns": 61_000 * MS,
        "transport": STREAMABLE_HTTP,
    }


def _refusal_lines(row: str) -> list[str]:
    refusal = CASES[row].refusal
    if refusal is None:
        return []
    if refusal[0] == "container":
        return [json.dumps({"message": f"{CONTAINER_LINE}; the shared-browser ..."})]
    root, kind = refusal[1], refusal[2]
    return [
        json.dumps(
            {
                "message": f"The {root} is on {kind} storage (inside a Dropbox "
                f"folder); this server drives its own browser instead of "
                f"sharing one"
            }
        )
    ]


def _ineligible(row: str, *, daemon: bool = True) -> dict[str, Any]:
    """A valid record of the cell *row* in K3 (*daemon*) or K1 frozen."""
    case = CASES[row]
    return {
        "row": row,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": INELIGIBLE_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "environment": dict(case.environment) if case.environment else None,
        "runtime_environment": (
            dict(case.runtime_environment) if case.runtime_environment else None
        ),
        "arguments": list(case.arguments),
        "transport": case.transport,
        "observation_problems": [],
        "script_error": None,
        "case": case.as_record(),
        "host_a": {"pid": PID_A},
        "storage": {
            name: {"class": kind, "reason": "test"}
            for name, kind in zip(
                ("profile", "auth_root", "state_root"), case.storage, strict=True
            )
        },
        "lines": decision_lines(_refusal_lines(row) if daemon else []),
        "host": _http_host() if case.transport == STREAMABLE_HTTP else _stdio_host(),
        "calls": [_feed_call(NOW + 1.0, NOW + 3.0)],
        "requests": [_request("/feed/", NOW + 2.0)],
        "egress": {"forwarded": ["www.linkedin.com"], "refused": []},
        "owner_processes": [],
        "gate_processes": [],
        "frontend_processes": [_lifetime(PID_A, NOW)],
        "browser_roots": [[7001, NOW + 1.5, NOW + 5.0, PID_A, NOW]],
        "daemon_state_existed": False,
        "descriptor_present": False,
    }


def _owner_seen() -> dict[str, Any]:
    return {"alive": True, "lifetime": list(OWNER[:2]), "instance_id": OWNER[2]}


def _rival(*, daemon: bool = True) -> dict[str, Any]:
    """A valid H-R14 record in K3 (*daemon*) or K1 frozen: A1 through the
    owner's (or A's server's) first browser, B through its own, A2 through
    the owner's (or A's) second."""
    reader, reader_start = (OWNER[0], OWNER[1]) if daemon else (PID_A, NOW)
    record: dict[str, Any] = {
        "row": ROW_RIVAL,
        "mode": "daemon" if daemon else "direct",
        "platform": "linux",
        "idle_timeout_seconds": RIVAL_IDLE_TIMEOUT_SECONDS,
        "k2": dict(K2_NOT_APPLICABLE),
        "environment": dict(RIVAL_ENVIRONMENT),
        "runtime_environment": None,
        "arguments": [],
        "transport": STDIO,
        "observation_problems": [],
        "script_error": None,
        "host_a": {"pid": PID_A},
        "usernames": {"B": B_USERNAME, "A2": A2_USERNAME},
        "storage": {
            name: {"class": "local", "reason": "test"}
            for name in ("profile", "auth_root", "state_root")
        },
        "lines": decision_lines([FORWARDING_LINE] if daemon else []),
        "rival": {
            "made": True,
            "launched_ns": 5_000 * MS,
            "launched": NOW + 5.0,
            "pid": PID_B,
            "environment": dict(RIVAL_OVERRIDES),
            "host": _stdio_host(),
            "call": _person_call(NOW + 8.0, NOW + 15.0),
            "forwarded": False,
            "lines": decision_lines(
                [LIVE_RIVAL_LINE, f"{NO_OWNER_LINE} (incompatible); this server"]
                if daemon
                else []
            ),
            "quit_problems": [],
            "retained": False,
        },
        "before_a2": {
            "remaining": [],
            "unresolved": [],
            "lease": FREE,
            "seen_ns": 30_000 * MS,
        },
        "host": _stdio_host(),
        "calls": [
            _feed_call(NOW + 1.0, NOW + 3.0),
            _person_call(NOW + 40.0, NOW + 45.0),
        ],
        "requests": [
            _request("/feed/", NOW + 2.0),
            _request("/feed/", NOW + 9.5),
            *_pages(B_USERNAME, NOW + 12.0),
            *_pages(A2_USERNAME, NOW + 41.0),
        ],
        "egress": {"forwarded": ["www.linkedin.com"], "refused": []},
        "owner_processes": [_lifetime(OWNER[0], OWNER[1], PID_A)] if daemon else [],
        "gate_processes": [],
        "frontend_processes": [_lifetime(PID_A, NOW), _lifetime(PID_B, NOW + 5.1)],
        "browser_roots": [
            [7001, NOW + 1.5, NOW + 8.0, reader, reader_start],
            [7002, NOW + 9.0, NOW + 20.0, PID_B, NOW + 5.1],
            [7003, NOW + 39.0, None, reader, reader_start],
        ],
        "daemon_state_existed": daemon,
        "descriptor_present": daemon,
    }
    if daemon:
        record.update(
            owner_identified=list(OWNER),
            owner_before_b=_owner_seen(),
            owner_after_b=_owner_seen(),
            owner_after_a2=_owner_seen(),
            owner_lines_after_b={"idle_exit": 0, "stood_down": 0},
            owner_lines_after_a2={"idle_exit": 0, "stood_down": 0},
        )
    return record


def _findings(problems: list[str]) -> list[str]:
    return [p for p in problems if not p.startswith(INVALID)]


_ALL_CELLS = [pytest.param(row, id=row) for row in CASES]


@pytest.mark.parametrize("row", _ALL_CELLS)
@pytest.mark.parametrize("daemon", [True, False], ids=["K3", "K1"])
def test_each_valid_record_has_no_problem(row, daemon):
    assert ineligible_problems(_ineligible(row, daemon=daemon), daemon=daemon) == []
    assert problems_for(_ineligible(row, daemon=daemon), daemon=daemon) == []


@pytest.mark.parametrize("daemon", [True, False], ids=["K3", "K1"])
def test_a_valid_rival_record_has_no_problem(daemon):
    assert rival_problems(_rival(daemon=daemon), daemon=daemon) == []
    assert problems_for(_rival(daemon=daemon), daemon=daemon) == []


def _change(record: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    changed = copy.deepcopy(record)
    *heads, last = path.split(".")
    target = changed
    for head in heads:
        target = target[head]
    if value is _DELETE:
        del target[last]
    else:
        target[last] = value
    return changed


_DELETE = object()


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            ("owner_processes", [_lifetime(OWNER[0], OWNER[1], PID_A)]),
            "the row started 1 shared owner process(es)",
            id="an owner launched",
        ),
        pytest.param(
            ("gate_processes", [_lifetime(9001, NOW + 0.5, PID_A)]),
            "the row started 1 owner release gate(s), an owner start attempted",
            id="a release gate launched",
        ),
        pytest.param(
            ("daemon_state_existed", True),
            "daemon state was created for the row's auth root",
            id="daemon state created",
        ),
        pytest.param(
            ("descriptor_present", True),
            "a descriptor was published for the row's auth root",
            id="a descriptor published",
        ),
        pytest.param(
            ("lines", decision_lines([FORWARDING_LINE])),
            "the frontend forwarded to a shared owner",
            id="forwarding",
        ),
        pytest.param(
            ("lines", decision_lines([f"{NO_OWNER_LINE} (absent)"])),
            "the frontend went on to an election: ['no_owner']",
            id="an election",
        ),
        pytest.param(
            ("browser_roots", []),
            "1 of the row's read's 1 page(s) went through no browser the row's "
            "own server launched",
            id="a read credited without its browser",
        ),
        pytest.param(
            ("browser_roots", [[7001, NOW + 1.5, NOW + 5.0, OWNER[0], OWNER[1]]]),
            "1 of the row's read's 1 page(s) went through no browser the row's "
            "own server launched",
            id="a read through an owner's browser",
        ),
        pytest.param(
            ("calls", [{**_feed_call(NOW + 1.0, NOW + 3.0), "read_the_post": False}]),
            "the row's read did not return the post",
            id="no post",
        ),
        pytest.param(
            ("host", {**_stdio_host(), "killed_by_harness": True}),
            "the host's quit was not a normal EOF exit: exited True, status 0, "
            "killed True",
            id="a host the harness killed",
        ),
    ],
)
def test_an_ineligible_cell_holds_each_of_its_observations(change, finding):
    record = _change(_ineligible(ROW_DISABLED_ENV), *change)
    problems = ineligible_problems(record, daemon=True)
    assert finding in problems, problems
    assert finding in _findings(problems)


@pytest.mark.parametrize(
    ("row", "lines", "finding"),
    [
        pytest.param(
            ROW_CONTAINER,
            [],
            "the frontend said 0 time(s), not once, that a container ignores "
            "DAEMON_ENABLED",
            id="container silent",
        ),
        pytest.param(
            ROW_CONTAINER,
            _refusal_lines(ROW_CONTAINER) * 2,
            "the frontend said 2 time(s), not once, that a container ignores "
            "DAEMON_ENABLED",
            id="container twice",
        ),
        pytest.param(
            ROW_SYNCED,
            [],
            "the frontend did not refuse once for the profile directory on "
            "synced storage: []",
            id="storage silent",
        ),
        pytest.param(
            ROW_STATE_SYNCED,
            _refusal_lines(ROW_SYNCED),
            "the frontend did not refuse once for the daemon state directory on "
            "synced storage: [['profile directory', 'synced']]",
            id="the wrong root",
        ),
        pytest.param(
            ROW_UNKNOWN,
            _refusal_lines(ROW_SYNCED),
            "the frontend did not refuse once for the profile directory on "
            "unknown storage: [['profile directory', 'synced']]",
            id="the wrong class",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            _refusal_lines(ROW_CONTAINER),
            "the frontend refused for a reason the cell does not declare: "
            "{'container': 1, 'storage': []}",
            id="an undeclared reason",
        ),
        pytest.param(
            ROW_CONTAINER,
            _refusal_lines(ROW_CONTAINER) + _refusal_lines(ROW_SYNCED),
            "the frontend also refused for its storage: "
            "[['profile directory', 'synced']]",
            id="container and storage",
        ),
    ],
)
def test_k3_says_why_it_keeps_its_own_browser_once_and_for_the_declared_reason(
    row, lines, finding
):
    record = _change(_ineligible(row), "lines", decision_lines(lines))
    problems = ineligible_problems(record, daemon=True)
    assert finding in _findings(problems), problems


@pytest.mark.parametrize(
    ("row", "change", "invalid"),
    [
        pytest.param(
            ROW_SYNCED,
            (
                "storage",
                {
                    name: {"class": "local", "reason": "x"}
                    for name in ("profile", "auth_root", "state_root")
                },
            ),
            "the product's classifier read ['local', 'local', 'local'] for the "
            "profile, its auth root and the state root, not the cell's "
            "['synced', 'synced', 'local']",
            id="a plant not in effect",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("storage", {"error": "TimeoutError"}),
            "the storage classification was not read: 'TimeoutError'",
            id="a classification not read",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("environment", {EnvironmentKeys.DAEMON_ENABLED: "true"}),
            "the cell did not run its declared configuration",
            id="the setting not read back",
        ),
        pytest.param(
            ROW_NO_DAEMON,
            ("arguments", []),
            "the cell did not run its declared configuration",
            id="the argument not given",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("owner_processes", None),
            "the row's owner and release-gate lifetimes were not recorded",
            id="launches unrecorded",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("daemon_state_existed", None),
            "the row's daemon state and descriptor were not recorded",
            id="state unrecorded",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("browser_roots", [[7001, NOW + 1.5, NOW + 5.0, None, None]]),
            "the row's browsers and who launched them were not recorded, so the "
            "row's read cannot be credited",
            id="an unattributable browser",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("frontend_processes", []),
            "the row's own server was never seen among the row's processes",
            id="the server never seen",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("requests", []),
            "the row's read asked the origin for no page of its own",
            id="no page",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("lines", _DELETE),
            "what the server said was not recorded",
            id="lines unrecorded",
        ),
        pytest.param(
            ROW_DISABLED_ENV,
            ("case", {"plant": "x"}),
            "the record does not name the cell's own case",
            id="another case",
        ),
    ],
)
def test_missing_or_misconfigured_evidence_is_invalid_and_never_a_finding(
    row, change, invalid
):
    record = _change(_ineligible(row), *change)
    problems = invalid_evidence(ineligible_problems(record, daemon=True))
    assert any(problem.startswith(INVALID + invalid) for problem in problems), problems


def test_a_frozen_direct_column_that_asked_about_sharing_is_invalid():
    record = _change(
        _ineligible(ROW_CONTAINER, daemon=False),
        "lines",
        decision_lines(_refusal_lines(ROW_CONTAINER)),
    )
    problems = ineligible_problems(record, daemon=False)
    assert invalid_evidence(problems) == [
        f"{INVALID}the frozen Direct column asked whether to share a browser: "
        f"{{'container': 1, 'storage': []}}"
    ]


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            ("interrupted", False),
            "the HTTP server's interrupt was not shown delivered: None",
            id="not interrupted",
        ),
        pytest.param(
            ("killed_by_harness", True),
            "the HTTP server's quit was not a normal exit on its interrupt: exited "
            "True, status 0, killed True",
            id="killed",
        ),
        pytest.param(
            ("exit_seen_ns", 1),
            "the interrupt and the exit after it are not recorded in order",
            id="out of order",
        ),
    ],
)
def test_an_http_cell_holds_its_host_to_a_normal_interrupted_exit(change, finding):
    record = _ineligible(ROW_HTTP)
    record["host"][change[0]] = change[1]
    assert finding in ineligible_problems(record, daemon=True)


def test_an_http_cell_is_not_judged_by_stdin():
    # An HTTP host has no stdin to close: the stdio checks would fail it.
    assert ineligible_problems(_ineligible(ROW_HTTP), daemon=True) == []
    stdio = _change(_ineligible(ROW_DISABLED_ENV), "host", _http_host())
    assert any("stdin" in p for p in ineligible_problems(stdio, daemon=True))


# --- Verdicts: H-R14 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            ("rival.forwarded", True),
            "B forwarded to the owner of another configuration",
            id="B forwarded",
        ),
        pytest.param(
            ("rival.lines", decision_lines([f"{NO_OWNER_LINE} (incompatible)"])),
            "B fell back 0 time(s), not once after one probe",
            id="B never fell back",
        ),
        pytest.param(
            (
                "rival.lines",
                decision_lines(
                    [LIVE_RIVAL_LINE, LIVE_RIVAL_LINE, f"{NO_OWNER_LINE} (x)"]
                ),
            ),
            "B fell back 2 time(s), not once after one probe",
            id="B fell back twice",
        ),
        pytest.param(
            (
                "rival.lines",
                decision_lines(
                    [ASK_AGAIN_LINE, SILENT_RIVAL_LINE, f"{NO_OWNER_LINE} (x)"]
                ),
            ),
            "B probed the rival again or wrote it off as leftovers instead of "
            "leaving it alone",
            id="B probed again",
        ),
        pytest.param(
            ("rival.lines", decision_lines([LIVE_RIVAL_LINE])),
            "B did not warn once that it drives its own browser: 0",
            id="B never warned",
        ),
        pytest.param(
            ("rival.call", {**_person_call(NOW + 8.0, NOW + 15.0), "is_error": True}),
            "B's read did not return its own profile",
            id="B's read failed",
        ),
        pytest.param(
            ("rival.quit_problems", ["the harness had to kill the server"]),
            "B's quit: ['the harness had to kill the server']",
            id="B killed",
        ),
        pytest.param(
            (
                "owner_after_a2",
                {"alive": True, "lifetime": [9999, NOW + 30.0], "instance_id": "new"},
            ),
            "the owner A reads through is not the one A1 reached at after_a2: "
            "[9999, 1800000030.0], None",
            id="the owner replaced",
        ),
        pytest.param(
            ("owner_after_b", {**_owner_seen(), "alive": False}),
            "the owner A reads through is not the one A1 reached at after_b: "
            "[6161, 1799999950.0], None",
            id="the owner gone after B",
        ),
        pytest.param(
            ("owner_lines_after_b", {"idle_exit": 0, "stood_down": 1}),
            "the owner stood down while B ran",
            id="a turnover",
        ),
        pytest.param(
            (
                "owner_processes",
                [
                    _lifetime(OWNER[0], OWNER[1], PID_A),
                    _lifetime(9002, NOW + 6.0, PID_B),
                ],
            ),
            "another owner was launched beside the one A elected: [[9002, "
            "1800000006.0]]",
            id="B launched a second owner",
        ),
        pytest.param(
            (
                "browser_roots",
                [
                    [7001, NOW + 1.5, NOW + 8.0, OWNER[0], OWNER[1]],
                    [7003, NOW + 9.0, None, OWNER[0], OWNER[1]],
                ],
            ),
            "3 of B's read's 3 page(s) went through no browser B's own server launched",
            id="B's read credited without B's browser",
        ),
        pytest.param(
            (
                "browser_roots",
                [
                    [7001, NOW + 1.5, NOW + 8.0, OWNER[0], OWNER[1]],
                    [7002, NOW + 9.0, None, PID_B, NOW + 5.1],
                ],
            ),
            "3 of A2's 3 page(s) went through no browser the owner A1 reached launched",
            id="A2 through B's browser",
        ),
        pytest.param(
            ("lines", decision_lines([])),
            "A's frontend did not forward to the owner it elected",
            id="A never forwarded",
        ),
    ],
)
def test_the_rival_row_holds_each_of_its_observations(change, finding):
    record = _change(_rival(), *change)
    problems = rival_problems(record, daemon=True)
    assert finding in _findings(problems), problems


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        pytest.param(
            ("owner_processes", [_lifetime(9002, NOW + 6.0, PID_B)]),
            "the row started 1 shared owner process(es)",
            id="an owner in Direct",
        ),
        pytest.param(
            (
                "browser_roots",
                [
                    [7001, NOW + 1.5, NOW + 8.0, PID_A, NOW],
                    [7002, NOW + 9.0, None, PID_B, NOW + 5.1],
                ],
            ),
            "3 of A2's 3 page(s) went through no browser A's own server launched",
            id="A2 through B's browser",
        ),
        pytest.param(
            ("rival.forwarded", True),
            "B forwarded to the owner of another configuration",
            id="B forwarded",
        ),
    ],
)
def test_the_frozen_rival_row_holds_its_observations(change, finding):
    record = _change(_rival(daemon=False), *change)
    problems = rival_problems(record, daemon=False)
    assert finding in _findings(problems), problems


@pytest.mark.parametrize(
    ("change", "invalid"),
    [
        pytest.param(
            ("owner_lines_after_b", {"idle_exit": 1, "stood_down": 1}),
            "the owner idled out before A2: the idle timeout is too small for this "
            "runner",
            id="an idle exit before A2",
        ),
        pytest.param(
            ("rival", None),
            "the rival host never ran: None",
            id="no rival",
        ),
        pytest.param(
            ("rival.environment", {EnvironmentKeys.LOGIN_TIMEOUT: "1800"}),
            "B did not run with its declared difference",
            id="no difference",
        ),
        pytest.param(
            ("rival.launched_ns", 1_000 * MS),
            "B was not started after A1 ended",
            id="B before A1 ended",
        ),
        pytest.param(
            ("before_a2.remaining", [7002]),
            "B's browser was not shown gone before A2",
            id="B's browser not gone",
        ),
        pytest.param(
            ("before_a2.seen_ns", 50_000 * MS),
            "A2 was sent before B's browser was shown gone",
            id="A2 too early",
        ),
        pytest.param(
            ("calls", [_feed_call(NOW + 1.0, NOW + 3.0)]),
            "A2 was never sent",
            id="no A2",
        ),
        pytest.param(
            ("owner_identified", None),
            "the owner was never identified",
            id="no owner",
        ),
        pytest.param(
            ("environment", {EnvironmentKeys.LOGIN_TIMEOUT: "1801"}),
            "the cell did not run its declared configuration",
            id="A's setting",
        ),
        pytest.param(
            ("rival.retained", True),
            "B's server is not shown gone after its quit",
            id="B retained",
        ),
        pytest.param(
            ("owner_lines_after_b", {}),
            "the owner's log was not read after B",
            id="the owner's log unread",
        ),
        pytest.param(
            (
                "calls",
                [
                    _feed_call(NOW + 1.0, NOW + 3.0),
                    _person_call(NOW + 40.0, NOW + 45.0),
                    _person_call(NOW + 46.0, NOW + 47.0),
                ],
            ),
            "A read 2 profiles, not the one A2",
            id="two reads after B",
        ),
        pytest.param(
            ("owner_processes", None),
            "the row's owner and release-gate lifetimes were not recorded",
            id="launches unrecorded",
        ),
    ],
)
def test_missing_rival_evidence_is_invalid_and_never_a_finding(change, invalid):
    record = _change(_rival(), *change)
    problems = invalid_evidence(rival_problems(record, daemon=True))
    assert any(problem.startswith(INVALID + invalid) for problem in problems), problems


def _a2_at(began: float, *, daemon: bool = True) -> dict[str, Any]:
    """A rival record whose A2 was sent at *began*, A1 at ``NOW + 1``, and
    whose owner was gone after A2."""
    record = _rival(daemon=daemon)
    record["calls"][1] = _person_call(began, began + 5.0)
    record["requests"] = [
        *[r for r in record["requests"] if A2_USERNAME not in r["path"]],
        *_pages(A2_USERNAME, began + 1.0),
    ]
    if daemon:
        record["owner_after_a2"] = {**_owner_seen(), "alive": False}
    return record


def test_an_a2_sent_near_the_owner_s_idle_deadline_is_invalid_and_never_a_finding():
    """A1 sent at NOW + 1: the owner cannot idle out before NOW + 1 + the
    timeout, so an A2 inside the margin before that says nothing of B."""
    due = 1.0 + RIVAL_IDLE_TIMEOUT_SECONDS - RIVAL_IDLE_MARGIN_SECONDS
    late = rival_problems(_a2_at(NOW + due), daemon=True)
    assert late == [f"{INVALID}{NEAR_IDLE}"]
    early = rival_problems(_a2_at(NOW + due - 1.0), daemon=True)
    assert any("the owner A reads through is not" in p for p in _findings(early))
    assert NEAR_IDLE not in " ".join(early)


def test_an_owner_that_idled_out_before_a2_was_answered_is_invalid_not_a_finding():
    """A2 sent in time, but the owner's log shows its own idle exit by A2's
    end: whatever A2 met says nothing of B."""
    record = _a2_at(NOW + 40.0)
    record["owner_lines_after_a2"] = {"idle_exit": 1, "stood_down": 1}
    assert rival_problems(record, daemon=True) == [
        f"{INVALID}{eligibility_rows.IDLED_BEFORE_A2}"
    ]
    record["owner_lines_after_a2"] = {"idle_exit": 0, "stood_down": 0}
    assert any(
        "the owner A reads through is not" in p
        for p in _findings(rival_problems(record, daemon=True))
    )
    del record["owner_lines_after_a2"]
    assert rival_problems(record, daemon=True) == [
        f"{INVALID}the owner's log was not read after A2"
    ]


def test_what_b_did_to_the_owner_stays_judged_when_a2_meets_an_idle_exit():
    """B turned the owner over; A2, sent in time, is answered by a successor
    that later idles out. The idle exit leaves A2 unjudged, never B."""
    record = _a2_at(NOW + 40.0)
    record["owner_after_b"] = {**_owner_seen(), "alive": False}
    record["owner_lines_after_b"] = {"idle_exit": 0, "stood_down": 1}
    record["owner_lines_after_a2"] = {"idle_exit": 1, "stood_down": 1}
    problems = rival_problems(record, daemon=True)
    assert problems[-1] == f"{INVALID}{eligibility_rows.IDLED_BEFORE_A2}"
    assert "the owner stood down while B ran" in problems
    assert any("not the one A1 reached at after_b" in p for p in problems)


def test_what_b_did_to_the_owner_stays_judged_when_a2_is_never_sent():
    record = _rival()
    record["calls"] = record["calls"][:1]
    record["owner_after_b"] = {**_owner_seen(), "alive": False}
    record["owner_lines_after_b"] = {"idle_exit": 0, "stood_down": 1}
    problems = rival_problems(record, daemon=True)
    assert problems[-1] == f"{INVALID}A2 was never sent"
    assert "the owner stood down while B ran" in problems
    assert any("not the one A1 reached at after_b" in p for p in problems)
    # B never observed: nothing about it is judged.
    del record["owner_after_b"]
    assert not any("stood down" in p for p in rival_problems(record, daemon=True))


def test_an_owner_that_idled_out_while_b_ran_is_invalid_never_a_finding():
    """B ends after A's owner idled out on its own: the owner is gone after
    B, the log says idle and not stand-down, A2 is not sent. Nothing of B is
    a finding; a stand-down line beside it still is."""
    record = _rival()
    record["calls"] = record["calls"][:1]
    record["owner_after_b"] = {**_owner_seen(), "alive": False}
    record["owner_lines_after_b"] = {"idle_exit": 1, "stood_down": 0}
    problems = rival_problems(record, daemon=True)
    assert _findings(problems) == []
    assert any("idled out before A2" in p for p in problems)
    record["owner_lines_after_b"] = {"idle_exit": 1, "stood_down": 1}
    assert _findings(rival_problems(record, daemon=True)) == [
        "the owner stood down while B ran"
    ]
    del record["owner_lines_after_b"]
    assert _findings(rival_problems(record, daemon=True)) == []


def test_an_owner_launched_after_a2_was_sent_is_judged_with_a2():
    record = _rival()
    record["owner_processes"] = [
        _lifetime(OWNER[0], OWNER[1], PID_A),
        _lifetime(9002, NOW + 42.0, PID_A),
    ]
    assert rival_problems(record, daemon=True) == [
        "another owner was launched beside the one A elected: [[9002, 1800000042.0]]"
    ]


def test_the_frozen_direct_rival_has_no_idle_deadline():
    due = 1.0 + RIVAL_IDLE_TIMEOUT_SECONDS
    assert rival_problems(_a2_at(NOW + due, daemon=False), daemon=False) == []


@pytest.mark.parametrize(
    ("change", "invalid"),
    [
        pytest.param(
            ("lines", decision_lines([FORWARDING_LINE])),
            "the frozen Direct A asked about an owner",
            id="A forwarded",
        ),
        pytest.param(
            ("rival.lines", decision_lines([LIVE_RIVAL_LINE])),
            "the frozen Direct B asked about an owner",
            id="B fell back",
        ),
    ],
)
def test_a_frozen_direct_host_that_asked_about_an_owner_is_invalid(change, invalid):
    record = _change(_rival(daemon=False), *change)
    problems = invalid_evidence(rival_problems(record, daemon=False))
    assert any(problem.startswith(INVALID + invalid) for problem in problems), problems


@pytest.mark.parametrize(
    ("record", "daemon", "idle"),
    [
        pytest.param(
            _ineligible(ROW_DISABLED_ENV),
            True,
            INELIGIBLE_IDLE_TIMEOUT_SECONDS,
            id="R12",
        ),
        pytest.param(_rival(), True, RIVAL_IDLE_TIMEOUT_SECONDS, id="R14"),
    ],
)
def test_a_cell_run_with_another_idle_timeout_fails(record, daemon, idle):
    changed = _change(record, "idle_timeout_seconds", 20.0)
    assert (
        f"the row ran with an idle timeout of 20.0, not the declared {idle}"
        in problems_for(changed, daemon=daemon)
    )


@pytest.mark.parametrize("daemon", [True, False], ids=["K3", "K1"])
def test_an_a2_that_did_not_return_its_profile_fails(daemon):
    record = _rival(daemon=daemon)
    record["calls"][1]["is_error"] = True
    assert "A2 did not return its profile" in _findings(
        rival_problems(record, daemon=daemon)
    )


def test_a_rival_that_fell_back_silently_is_still_one_fallback():
    record = _change(
        _rival(),
        "rival.lines",
        decision_lines([SILENT_RIVAL_LINE, f"{NO_OWNER_LINE} (incompatible)"]),
    )
    assert rival_problems(record, daemon=True) == []


# --- K0 and the comparisons ----------------------------------------------------------


def test_a_repeat_reads_as_its_reference_and_an_invalid_one_is_refused():
    reference = _ineligible(ROW_SYNCED)
    assert semantic_differences(reference, copy.deepcopy(reference)) == []
    changed = _change(reference, "lines", decision_lines(_refusal_lines(ROW_CONTAINER)))
    assert semantic_differences(reference, changed)[0].startswith(
        "the repeat record is not valid"
    )
    rival = _rival()
    silent = _change(
        rival,
        "rival.lines",
        decision_lines([SILENT_RIVAL_LINE, f"{NO_OWNER_LINE} (incompatible)"]),
    )
    assert semantic_differences(rival, silent) == [], "live or silent is timing"


def test_k3_is_held_to_k1_only_from_two_valid_records():
    direct, daemon = _ineligible(ROW_HTTP, daemon=False), _ineligible(ROW_HTTP)
    assert comparison_refusals(direct, daemon) == []
    assert comparison_refusals(None, daemon) == [
        "the Direct record is not valid: ['the row kept no record']"
    ]
    assert comparison_refusals(_rival(daemon=False), _rival()) == []


# --- Declarations ----------------------------------------------------------------------


def test_every_row_here_is_declared_held_to_direct_or_rival_and_judged():
    for row, case in CASES.items():
        declared = harness.ROWS[row]
        assert lifecycle_problems(row, declared) == []
        assert declared.eligible is False and declared.coordination is True
        assert declared.environment == case.environment
        assert declared.runtime_environment == case.runtime_environment
        assert declared.arguments == case.arguments
        assert declared.transport == case.transport
        assert declared.preservation == ORDINARY
        assert declared.idle_timeout == INELIGIBLE_IDLE_TIMEOUT_SECONDS
        assert harness.ROW_VERDICTS[row] is eligibility_rows.problems_for
    rival = harness.ROWS[ROW_RIVAL]
    assert lifecycle_problems(ROW_RIVAL, rival) == []
    assert (rival.eligible, rival.coordination, rival.rival) == (True, True, True)
    assert rival.environment == RIVAL_ENVIRONMENT
    assert rival.idle_timeout == RIVAL_IDLE_TIMEOUT_SECONDS
    # One fingerprinted field, one second apart; the minimum hold the same.
    assert set(RIVAL_OVERRIDES) == {EnvironmentKeys.LOGIN_TIMEOUT}
    assert RIVAL_OVERRIDES != {k: RIVAL_ENVIRONMENT[k] for k in RIVAL_OVERRIDES}


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (
            {"transport": "sse"},
            "no transport 'sse'",
        ),
        (
            {"transport": STREAMABLE_HTTP, "termination": "eof"},
            "a loss on a host with no pipes to lose",
        ),
        (
            {"transport": STREAMABLE_HTTP, "scenarios": True},
            "an HTTP host that combines with other scenarios",
        ),
        (
            {"arguments": ("--transport", "stdio")},
            "a transport given as an argument rather than declared",
        ),
        (
            {"rival": True, "coordination": False},
            "a rival host with no coordination seams to start it",
        ),
        (
            {"rival": True, "eligible": False},
            "a rival host beside a configuration with no owner",
        ),
        (
            {"coordination": True, "script": None},
            "a coordination reading with no script to take it",
        ),
        (
            {"rival": True, "scenarios": True},
            "a rival host that combines with other scenarios",
        ),
    ],
)
def test_a_declaration_that_does_not_hold_together_is_refused(change, problem):
    declared = dataclasses.replace(
        RowLifecycle(
            recorded=True,
            scenarios=False,
            script=eligibility_rows.rival_script,
            coordination=True,
        ),
        **change,
    )
    assert problem in lifecycle_problems(ROW_RIVAL, declared)


async def test_an_ineligible_row_refuses_a_column_expecting_an_owner_before_anything(
    monkeypatch, tmp_path
):
    claimed = MagicMock()
    monkeypatch.setattr(harness, "claim_account", claimed)
    with pytest.raises(ValueError, match="no column of it may expect an owner"):
        await measure_host_quit_row(
            profile=tmp_path / "p",
            experiment="K3",
            daemon=True,
            egress=(MagicMock(), MagicMock()),
            log=EventLog(tmp_path / "evidence", run="refused"),
            work_dir=tmp_path / "row",
            row=ROW_DISABLED_ENV,
            expect_owner=True,
        )
    claimed.assert_not_called()


# --- The wiring: the real row entry on modelled actors ---------------------------------


class _Scene(_CalibrationScene):
    """The real row entry on the calibration's modelled actors and real
    origin, for the rows here. The host double says ``lines`` on its stderr
    and is ``PID_A``; a host started without a script is the rival, ``PID_B``,
    saying ``rival_lines``. The watcher saw each server launch the browser
    its reads went through, unless a test says otherwise."""

    def __init__(self, monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
        super().__init__(monkeypatch, tmp_path, profile, origin, certificates)
        self.lines: list[str] = []
        self.rival_lines: list[str] = []
        #: Whether the rival's server is seen launching a browser of its own.
        self.rival_browser = True
        self.hosts: list[dict[str, Any]] = []
        self.staged_container: list[str | None] = []
        self.browser_key = harness.ActorAccount(profile[0]).browser_key
        began = time.time() - 30.0
        self.began = began
        self.records = [
            _start(PID_A, began, os.getpid(), "frontend", _SERVER, t=began),
            _start(7000, began + 0.1, PID_A, "driver", ["node"], t=began),
            _start(7001, began + 0.2, 7000, "browser", ["chrome"], t=began),
        ]
        monkeypatch.setattr(_Watcher, "records", self.records)
        monkeypatch.setattr(
            harness,
            "retire_daemon_state",
            lambda *a: DaemonCleanup("dir", False, False, True, True),
        )
        _temporary_home(monkeypatch, tmp_path / "home")
        monkeypatch.setattr(harness, "run_http_host_session", self.http_host)
        monkeypatch.setattr(
            harness,
            "read_lock",
            lambda _path: {"now": None, "answer": {"state": "free"}},
        )

        async def stage(*_args, **_kwargs):
            self.staged_container.append(os.environ.get("LINKEDIN_MCP_CONTAINER"))
            return profile[1]

        monkeypatch.setattr(harness, "stage_signed_in_session", stage)

    async def host(
        self,
        command,
        *,
        env,
        on_stderr,
        tool,
        arguments,
        after_call=None,
        row_script=None,
        transport=STDIO,
        **kw,
    ):
        rival = row_script is None and kw.get("started") is None
        self.hosts.append(
            {"command": list(command), "env": dict(env), "tool": tool, "rival": rival}
        )
        session = harness.HostSession(
            alive_before_quit=True,
            stdin_closed=transport == STDIO,
            exited_on_quit=True,
            exit_code=0,
            transport=transport,
            interrupted=True if transport != STDIO else None,
        )
        session.pid = PID_B if rival else PID_A
        if rival:
            # Seen when it starts, as the watcher would see it.
            now = time.time()
            self.records.append(
                _start(PID_B, now, os.getpid(), "frontend", _SERVER, t=now)
            )
            if self.rival_browser:
                self.records += [
                    _start(7100, now + 0.01, PID_B, "driver", ["node"], t=now),
                    _start(7101, now + 0.02, 7100, "browser", ["chrome"], t=now),
                ]
        if kw.get("on_process") is not None:
            kw["on_process"](SimpleNamespace(pid=session.pid, returncode=0))
        if kw.get("started") is not None:
            kw["started"](PID_A)
        for line in self.rival_lines if rival else self.lines:
            session.stderr.append(line)
            on_stderr(line)
        session.tool = await self._call(session, tool, arguments)
        if after_call is not None:
            await after_call()
        if row_script is not None:

            async def call(name, arguments):
                summary = await self._call(session, name, arguments)
                session.scripted.append(summary)
                return summary

            try:
                await row_script(call, SimpleNamespace(pid=PID_A))
            except Exception as exc:  # noqa: BLE001 - as the real session keeps it
                session.script_error = f"{type(exc).__name__}: {exc}"
        session.eof_monotonic_ns = time.monotonic_ns()
        session.interrupt_monotonic_ns = session.eof_monotonic_ns
        session.exit_seen_monotonic_ns = time.monotonic_ns()
        return session

    async def http_host(self, *args, **kwargs):
        return await self.host(*args, transport=STREAMABLE_HTTP, **kwargs)

    async def run(self, row: str, *, daemon: bool = True):  # ty: ignore[invalid-method-override]
        return await measure_host_quit_row(
            profile=self.tmp_path / "auth" / "profile",
            experiment="K3" if daemon else "K1",
            daemon=daemon,
            egress=(self.origin, SimpleNamespace(url="x", decisions=[])),  # ty: ignore[invalid-argument-type]
            log=self.log,
            work_dir=self.tmp_path / "row",
            row=row,
        )


@pytest.fixture
def scene(monkeypatch, tmp_path, profile, origin, certificates):  # noqa: F811
    return _Scene(monkeypatch, tmp_path, profile, origin, certificates)


@pytest.mark.parametrize(
    "row", [ROW_DISABLED_ENV, ROW_NO_DAEMON, ROW_CONTAINER, ROW_HTTP]
)
async def test_a_refused_configuration_passes_through_the_real_row_entry(scene, row):
    case = CASES[row]
    scene.lines = _refusal_lines(row)
    result = await scene.run(row)

    assert result.failures == [], result.failures
    assert result.vector is not None and result.vector.owner_published is False
    record = scene.published()["record"]
    assert record["problems"] == []
    assert ineligible_problems(record, daemon=True) == []
    [host] = scene.hosts
    # The declared configuration, in the actors' own environment and command.
    assert host["env"][EnvironmentKeys.DAEMON_ENABLED] == (
        "false" if row == ROW_DISABLED_ENV else "true"
    )
    assert host["command"][len(host["command"]) - len(case.arguments) :] == list(
        case.arguments
    )
    container = "true" if row == ROW_CONTAINER else None
    assert host["env"].get("LINKEDIN_MCP_CONTAINER") == container
    assert scene.staged_container == [container]
    assert scene.preservation.await_args.kwargs["environment"] == (
        {"LINKEDIN_MCP_CONTAINER": "true"} if row == ROW_CONTAINER else {}
    )
    assert "LINKEDIN_MCP_CONTAINER" not in os.environ
    assert record["host"]["transport"] == case.transport


@pytest.mark.parametrize(
    ("plant", "findings"),
    [
        pytest.param(
            "owner",
            (
                "a row that must stay Direct started a shared owner process (the "
                "watcher saw it; publication is not required)",
                f"{ROW_DISABLED_ENV}: the row started 1 shared owner process(es)",
            ),
            id="an owner launched",
        ),
        pytest.param(
            "gate",
            (
                "a row that must stay Direct started the shared owner's release "
                "gate (an attempted owner start, released or not)",
                f"{ROW_DISABLED_ENV}: the row started 1 owner release gate(s), an "
                f"owner start attempted",
            ),
            id="a release gate launched",
        ),
        pytest.param(
            "forwarding",
            (
                "a row that must stay Direct reached a shared owner",
                f"{ROW_DISABLED_ENV}: the frontend forwarded to a shared owner",
            ),
            id="forwarding",
        ),
        pytest.param(
            "state",
            (
                "a row that must stay Direct left daemon state for its auth root: dir",
                f"{ROW_DISABLED_ENV}: daemon state was created for the row's auth root",
            ),
            id="daemon state created",
        ),
        pytest.param(
            "roots",
            ("O1: a second browser ran on the profile, or O1 is unknown",),
            id="a second browser root",
        ),
        pytest.param(
            "no browser",
            (
                f"{ROW_DISABLED_ENV}: 1 of the row's read's 1 page(s) went through "
                f"no browser the row's own server launched",
            ),
            id="a read credited without its browser",
        ),
    ],
)
async def test_a_coordination_effect_fails_the_real_row_entry(
    scene, monkeypatch, plant, findings
):
    if plant == "owner":
        scene.records.append(
            _start(6161, scene.began + 2.0, PID_A, "owner", _OWNER_CMD, t=scene.began)
        )
    elif plant == "gate":
        gate = harness.gate_script(harness.REPO_ROOT)
        scene.records.append(
            _start(
                9001,
                scene.began + 2.0,
                PID_A,
                "owner",
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-u",
                    str(gate),
                    "a" * 64,
                    "--",
                    *_OWNER_CMD,
                ],
                t=scene.began,
            )
        )
    elif plant == "forwarding":
        scene.lines = [FORWARDING_LINE]
    elif plant == "state":
        monkeypatch.setattr(
            harness,
            "retire_daemon_state",
            lambda *a: DaemonCleanup("dir", True, False, True, True),
        )
    elif plant == "roots":
        monkeypatch.setattr(
            _Watcher, "summary", {**_SETTLED, "max_roots": {scene.browser_key: 2}}
        )
    else:
        del scene.records[1:3]
    result = await scene.run(ROW_DISABLED_ENV)

    for finding in findings:
        assert finding in result.failures, result.failures


async def test_the_frozen_rival_row_passes_through_the_real_row_entry(scene):
    result = await scene.run(ROW_RIVAL, daemon=False)

    assert result.failures == [], result.failures
    record = scene.published()["record"]
    assert record["problems"] == []
    host_a, host_b = scene.hosts
    assert not host_a["rival"] and host_b["rival"]
    for name, value in RIVAL_ENVIRONMENT.items():
        assert host_a["env"][name] == value
    assert host_b["env"][EnvironmentKeys.LOGIN_TIMEOUT] == "1801"
    assert (
        host_b["env"][EnvironmentKeys.BROWSER_MIN_HOLD]
        == RIVAL_ENVIRONMENT[EnvironmentKeys.BROWSER_MIN_HOLD]
    )
    assert host_b["tool"] == PERSON_TOOL
    assert record["rival"]["environment"] == RIVAL_OVERRIDES
    assert record["rival"]["pid"] == PID_B
    assert [call["tool"] for call in record["calls"]] == ["get_feed", PERSON_TOOL]


async def test_a_rival_that_forwards_fails_the_real_row_entry(scene):
    scene.rival_lines = [FORWARDING_LINE]
    result = await scene.run(ROW_RIVAL, daemon=False)

    assert f"{ROW_RIVAL}: B forwarded to the owner of another configuration" in (
        result.failures
    ), result.failures


async def test_a_rival_whose_read_no_browser_of_its_own_made_fails(scene):
    scene.rival_browser = False
    result = await scene.run(ROW_RIVAL, daemon=False)

    assert (
        f"{ROW_RIVAL}: 3 of B's read's 3 page(s) went through no browser B's own "
        f"server launched" in result.failures
    ), result.failures


# --- H-R14's script on a modelled owner ----------------------------------------------


class _ModelledRival:
    """A ``RowContext`` for the rival script in daemon mode: an identified
    owner, readings that say what the test sets, and every step in order."""

    def __init__(self, tmp_path: Path) -> None:
        self.steps: list[str] = []
        self.settled = True
        self.rival_answer: dict[str, Any] | None = {"made": True}
        self.overrides: dict[str, str] = {}
        self.record: dict[str, Any] = {"observation_problems": []}
        profile_dir = tmp_path / "auth" / "profile"
        profile_dir.mkdir(parents=True)
        owner = SimpleNamespace(
            pid=OWNER[0], create_time=OWNER[1], instance_id=OWNER[2]
        )

        async def call(name, arguments):
            self.steps.append(f"call {name} {arguments['linkedin_username']}")
            return {"tool": name}

        async def reading(label):
            self.steps.append(f"owner {label}")
            return {"label": label}

        async def settlement():
            self.steps.append("settlement")
            return (
                {"remaining": [], "unresolved": [], "lease": FREE}
                if self.settled
                else {"remaining": [7002], "unresolved": []}
            )

        async def rival(overrides, *, tool, arguments):
            self.steps.append(f"rival {tool} {arguments['linkedin_username']}")
            self.overrides = dict(overrides)
            return self.rival_answer

        self.ctx = RowContext(
            row=ROW_RIVAL,
            daemon=True,
            call=call,
            transport=SimpleNamespace(pid=PID_A),  # ty: ignore[invalid-argument-type]
            origin=MagicMock(),
            proxy=MagicMock(),
            account=harness.ActorAccount(profile_dir),
            record=self.record,
            owner=lambda: owner,  # ty: ignore[invalid-argument-type]
            browser_exe=None,
            browser_dir=tmp_path,
            _emit=lambda *_a, **_k: None,
            _gates=[],
            coordination=CoordinationSeams(
                host_output=lambda: [FORWARDING_LINE],
                owner_reading=reading,
                owner_log=lambda: ["serving"],
                settlement=settlement,
                rival=rival,
            ),
        )


def _wall_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    """What the rival script reads as the time."""
    monkeypatch.setattr(eligibility_rows, "time", SimpleNamespace(time=lambda: now))


async def test_the_rival_script_reads_the_owner_around_b_and_settles_before_a2(
    tmp_path, monkeypatch
):
    _temporary_home(monkeypatch, tmp_path / "home")
    _wall_clock(monkeypatch, OWNER[1] + 10.0)
    modelled = _ModelledRival(tmp_path)
    await rival_script(modelled.ctx)

    assert modelled.steps == [
        "owner before B",
        f"rival {PERSON_TOOL} {B_USERNAME}",
        "owner after B",
        "settlement",
        f"call {PERSON_TOOL} {A2_USERNAME}",
        "owner after A2",
    ]
    assert modelled.overrides == RIVAL_OVERRIDES
    record = modelled.record
    assert record["owner_identified"] == list(OWNER)
    assert record["owner_lines_after_b"] == {"idle_exit": 0, "stood_down": 0}
    assert record["owner_lines_after_a2"] == {"idle_exit": 0, "stood_down": 0}
    assert record["lines"]["forwarding"] == 1
    assert record["observation_problems"] == []


@pytest.mark.parametrize(
    ("change", "invalid", "steps"),
    [
        pytest.param(
            "unsettled",
            "B's browser was not shown gone before A2, so A2 was not sent",
            [
                "owner before B",
                f"rival {PERSON_TOOL} {B_USERNAME}",
                "owner after B",
                "settlement",
            ],
            id="B's browser not gone",
        ),
        pytest.param(
            "no rival",
            "the rival host did not end within",
            ["owner before B", f"rival {PERSON_TOOL} {B_USERNAME}"],
            id="no rival",
        ),
        pytest.param(
            "near idle",
            f"{NEAR_IDLE}, so A2 was not sent",
            [
                "owner before B",
                f"rival {PERSON_TOOL} {B_USERNAME}",
                "owner after B",
                "settlement",
            ],
            id="the owner's idle deadline close",
        ),
    ],
)
async def test_the_rival_script_sends_no_a2_on_evidence_it_cannot_stand_on(
    tmp_path, monkeypatch, change, invalid, steps
):
    _temporary_home(monkeypatch, tmp_path / "home")
    _wall_clock(
        monkeypatch,
        OWNER[1]
        + RIVAL_IDLE_TIMEOUT_SECONDS
        - RIVAL_IDLE_MARGIN_SECONDS
        + (0.0 if change == "near idle" else -1.0),
    )
    modelled = _ModelledRival(tmp_path)
    if change == "unsettled":
        modelled.settled = False
    elif change == "no rival":
        modelled.rival_answer = None
    await rival_script(modelled.ctx)

    assert modelled.steps == steps
    assert any(
        p.startswith(INVALID + invalid) for p in modelled.record["observation_problems"]
    )


# --- What stays with the models -----------------------------------------------------------


@pytest.mark.parametrize("branch", list(MODEL_COVERAGE))
def test_each_branch_left_to_the_models_names_tests_that_exist(branch):
    """A check of the mapping only: never counted as any branch's coverage,
    which comes from the mapped tests' own runs."""
    _row, nodes = MODEL_COVERAGE[branch]
    root = Path(__file__).resolve().parents[2]
    assert nodes
    for node in nodes:
        path, _, name = node.partition("::")
        assert name in _functions(root / path), node


def test_the_retry_and_settlement_lane_is_open_and_its_references_never_count():
    root = Path(__file__).resolve().parents[2]
    assert "stays open" in eligibility_rows.RIVAL_RETRY_OPEN
    assert not any("retry or settlement" in name for name in MODEL_COVERAGE)
    for node in eligibility_rows.RIVAL_RETRY_REFERENCES:
        path, _, name = node.partition("::")
        assert name in _functions(root / path), node


def test_the_cells_skip_only_where_they_cannot_run():
    assert CASES[ROW_HTTP].skip_reason("win32") == eligibility_rows.HTTP_NOT_ON_WINDOWS
    assert CASES[ROW_HTTP].skip_reason("linux") is None
    assert CASES[eligibility_rows.ROW_CLOUD].skip_reason("darwin") is None
    assert (
        CASES[eligibility_rows.ROW_CLOUD].skip_reason("linux")
        == eligibility_rows.CLOUD_ONLY_ON_MACOS
    )
    assert all(
        CASES[row].skip_reason(platform) is None
        for row in (ROW_DISABLED_ENV, ROW_NO_DAEMON, ROW_CONTAINER, ROW_SYNCED)
        for platform in ("win32", "linux", "darwin")
    )
    assert MUST_NOT_REPAIR != harness.ROWS[ROW_SYNCED].preservation
