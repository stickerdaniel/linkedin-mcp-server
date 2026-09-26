"""The host stub quits the way a host does: stdin EOF, then a wait.

Driven against stand-in stdio servers. One takes three seconds to close, which
is longer than the two seconds after which the MCP SDK's own stdio client
starts signalling, so a stub that killed its server would fail here and a
native row built on it would measure a kill while calling it a quit. Two more
answer the call and then end badly, one with a nonzero status on EOF and one
before the host ever quits; neither may read as a normal host quit.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from differential.harness import (
    READ_TOOL,
    actor_environment,
    claim_account,
    host_failures,
    run_host_session,
)
from differential.synthetic_origin import POST_MARKER
from linkedin_mcp_server import daemon_descriptor

_STAND_IN_SERVER = """
import os
import sys
import threading
import time
from contextlib import asynccontextmanager

from fastmcp import FastMCP

marker, ending = sys.argv[1], sys.argv[2]


@asynccontextmanager
async def closing(app):
    print("stand-in server up", file=sys.stderr, flush=True)
    try:
        yield {}
    finally:
        if ending == "slow":
            time.sleep(3)
            print("stand-in server closed", file=sys.stderr, flush=True)
        elif ending == "status":
            print("stand-in server failing its close", file=sys.stderr, flush=True)
            sys.stderr.flush()
            os._exit(7)


mcp = FastMCP("stand-in", lifespan=closing)


@mcp.tool
def %(tool)s(num_posts: int = 10) -> dict:
    if ending == "crash":
        # Dies right after answering, long before the host quits.
        threading.Timer(0.2, lambda: os._exit(9)).start()
    return {"url": "https://www.linkedin.com/feed/", "sections": {"feed": marker}}


mcp.run(transport="stdio", show_banner=False)
""" % {"tool": READ_TOOL}


async def _session(tmp_path: Path, ending: str, seen: list[str] | None = None):
    script = tmp_path / "stand_in_server.py"
    script.write_text(_STAND_IN_SERVER)

    async def linger() -> None:
        if ending == "crash":
            await asyncio.sleep(2)

    return await run_host_session(
        [sys.executable, str(script), POST_MARKER, ending],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=(seen.append if seen is not None else lambda _line: None),
        after_call=linger,
    )


async def test_a_host_quit_waits_for_a_slow_server_instead_of_killing_it(tmp_path):
    seen: list[str] = []
    session = await _session(tmp_path, "slow", seen)

    assert session.error is None, session.error
    assert session.tool is not None
    assert session.tool["read_the_post"] and not session.tool["is_error"]
    assert session.alive_before_quit is True and session.stdin_closed is True
    assert session.exited_on_quit is True
    assert session.killed_by_harness is False
    assert session.quit_seconds is not None and session.quit_seconds >= 3
    assert "stand-in server closed" in seen
    assert seen == session.stderr
    assert host_failures(session) == []


async def test_a_server_that_answers_then_exits_nonzero_is_not_a_normal_quit(
    tmp_path,
):
    session = await _session(tmp_path, "status")
    assert session.tool is not None and session.tool["read_the_post"]
    assert session.alive_before_quit is True
    assert session.exited_on_quit is True and session.exit_code == 7
    assert session.killed_by_harness is False
    assert any("status 7" in failure for failure in host_failures(session))


async def test_a_server_that_died_before_the_quit_is_not_a_normal_quit(tmp_path):
    session = await _session(tmp_path, "crash")
    assert session.tool is not None and session.tool["read_the_post"]
    assert session.alive_before_quit is False
    assert any("already gone" in failure for failure in host_failures(session))


def test_the_actor_environment_carries_the_row_and_drops_inherited_settings(
    tmp_path, monkeypatch
):
    # A CHROME_PATH left in the environment would quietly keep the server off
    # the daemon (a custom browser stays Direct), so K3 would measure K1.
    monkeypatch.setenv("CHROME_PATH", "/somewhere/chrome")
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")
    # A home of its own, so the account guard never looks at the real one.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(daemon_descriptor, "_account_home", lambda: home)
    (tmp_path / "auth").mkdir()
    account = claim_account(tmp_path / "auth" / "profile")

    env = actor_environment(
        account, "http://127.0.0.1:9", daemon=True, browsers=Path("/cache")
    )

    assert "CHROME_PATH" not in env
    assert "LINKEDIN_DEBUG_BRIDGE_COOKIE_SET" not in env
    assert env["USER_DATA_DIR"] == str(account.profile)
    assert env["PROXY_SERVER"] == "http://127.0.0.1:9"
    assert env["DAEMON_ENABLED"] == "true"
    assert env["HEADLESS"] == "true"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(Path("/cache"))
    assert (
        actor_environment(
            account, "http://127.0.0.1:9", daemon=False, browsers=Path("/cache")
        )["DAEMON_ENABLED"]
        == "false"
    )
