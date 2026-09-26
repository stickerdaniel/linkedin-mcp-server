"""The host stub quits the way a host does: stdin EOF, then a wait.

Driven against a stand-in stdio server that takes three seconds to close, which
is longer than the two seconds after which the MCP SDK's own stdio client
starts signalling. A stub that killed its server would therefore fail here,
and a native row built on it would measure a kill while calling it a quit.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from differential.harness import (
    READ_TOOL,
    actor_environment,
    claim_account,
    run_host_session,
)
from differential.synthetic_origin import POST_MARKER

_STAND_IN_SERVER = """
import sys
import time
from contextlib import asynccontextmanager

from fastmcp import FastMCP

marker = sys.argv[1]


@asynccontextmanager
async def closing_slowly(app):
    print("stand-in server up", file=sys.stderr, flush=True)
    try:
        yield {}
    finally:
        time.sleep(3)
        print("stand-in server closed", file=sys.stderr, flush=True)


mcp = FastMCP("stand-in", lifespan=closing_slowly)


@mcp.tool
def %(tool)s(num_posts: int = 10) -> dict:
    return {"url": "https://www.linkedin.com/feed/", "sections": {"feed": marker}}


mcp.run(transport="stdio", show_banner=False)
""" % {"tool": READ_TOOL}


async def test_a_host_quit_waits_for_a_slow_server_instead_of_killing_it(tmp_path):
    script = tmp_path / "stand_in_server.py"
    script.write_text(_STAND_IN_SERVER)
    seen: list[str] = []

    session = await run_host_session(
        [sys.executable, str(script), POST_MARKER],
        env=dict(os.environ),
        cwd=tmp_path,
        on_stderr=seen.append,
    )

    assert session.error is None, session.error
    assert session.tool is not None
    assert session.tool["read_the_post"] and not session.tool["is_error"]
    assert session.exited_on_quit is True
    assert session.killed_by_harness is False
    assert session.quit_seconds is not None and session.quit_seconds >= 3
    assert "stand-in server closed" in seen
    assert seen == session.stderr


def test_the_actor_environment_carries_the_row_and_drops_inherited_settings(
    tmp_path, monkeypatch
):
    # A CHROME_PATH left in the environment would quietly keep the server off
    # the daemon (a custom browser stays Direct), so K3 would measure K1.
    monkeypatch.setenv("CHROME_PATH", "/somewhere/chrome")
    monkeypatch.setenv("LINKEDIN_DEBUG_BRIDGE_COOKIE_SET", "bridge_core")
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
