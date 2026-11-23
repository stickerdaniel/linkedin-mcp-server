"""Two real stdio server processes on one auth root, as Claude Desktop runs them.

Process A answers the model: its ``open_login`` starts the login. Process B
answers the app: it never saw that call and reaches A's login only through the
relay socket in the shared auth root. Both run the real entry point through
``tests/helpers/login_relay_server.py``, which replaces only the login browser
with a recorded page; that launcher is the one place the stand-in exists.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mcp.types as mt
import pytest
from fastmcp import Client
from fastmcp.apps.config import UI_EXTENSION_ID
from fastmcp.client.transports import StdioTransport
from mcp.client import advertise

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not hasattr(socket, "AF_UNIX") or sys.platform == "win32",
        reason="the relay runs on Unix sockets",
    ),
]

_LAUNCHER = Path(__file__).parent / "helpers" / "login_relay_server.py"
_VIEW = "linkedin.dev/login-view"
_GATE_KEYS = (
    "LINKEDIN_MCP_REMOTE_TOOLS",
    "LINKEDIN_MCP_CHECK_FOR_UPDATES",
    "LINKEDIN_MCP_RUNTIME",
    "DAEMON_ENABLED",
    "USER_DATA_DIR",
    "AUTO_IMPORT_FROM_BROWSER",
)


@pytest.fixture
def root() -> Iterator[Path]:
    """A temporary directory short enough for the socket path."""
    base = Path(tempfile.mkdtemp(prefix="lrp-", dir="/tmp"))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _server(root: Path) -> StdioTransport:
    env = {key: value for key, value in os.environ.items() if key not in _GATE_KEYS}
    repo = str(Path(__file__).resolve().parents[1])
    env.update(
        HOME=str(root / "home"),
        USER_DATA_DIR=str(root / "auth" / "profile"),
        CHROME_PATH=sys.executable,
        AUTO_IMPORT_FROM_BROWSER="false",
        LINKEDIN_MCP_RUNTIME="docker",
        LINKEDIN_MCP_REMOTE_TOOLS="off",
        LINKEDIN_MCP_CHECK_FOR_UPDATES="false",
        LOGIN_RELAY_TEST_EVENTS=str(root / "events.jsonl"),
        LOGIN_RELAY_TEST_FINISH=str(root / "finish"),
        PYTHONPATH=os.pathsep.join(filter(None, [repo, env.get("PYTHONPATH")])),
    )
    return StdioTransport(
        command=sys.executable,
        args=[str(_LAUNCHER)],
        env=env,
        cwd=str(root),
        keep_alive=False,
    )


def _text(result: mt.CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, mt.TextContent))


def _reply(result: mt.CallToolResult) -> dict[str, Any]:
    return json.loads(_text(result))


def _images(result: mt.CallToolResult) -> list[mt.ImageContent]:
    return [c for c in result.content if isinstance(c, mt.ImageContent)]


async def _frame(client: Client, view: dict[str, str]) -> mt.CallToolResult:
    return await client.call_tool_mcp(
        "login_view_frame",
        {"grant": view["grant"], "attempt_id": view["attempt_id"], "budget_s": 1},
    )


async def _until(
    client: Client, view: dict[str, str], wanted: str
) -> mt.CallToolResult:
    for _ in range(40):
        result = await _frame(client, view)
        reply = _reply(result)
        if wanted in (reply.get("frame"), reply.get("state")):
            return result
    raise AssertionError(f"the view never said {wanted}: {reply}")


def _events(root: Path) -> list[list[Any]]:
    path = root / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


async def test_the_apps_process_drives_the_login_the_models_process_started(
    root: Path,
):
    (root / "home").mkdir()
    ui = [advertise(UI_EXTENSION_ID, {"mimeTypes": ["text/html;profile=mcp-app"]})]
    model = Client(_server(root), extensions=ui, init_timeout=90, timeout=60)
    async with model:
        opened = await model.call_tool_mcp("open_login", {})
        shown = opened.structured_content
        assert isinstance(shown, dict) and shown.get("view") == "shown", _text(opened)
        binding = {"attempt_id": shown["attempt_id"], "open_id": shown["open_id"]}

        async with Client(_server(root), init_timeout=90, timeout=60) as app:
            attached = await app.call_tool_mcp("login_view_attach", binding)
            meta = (attached.meta or {}).get(_VIEW)
            assert meta is not None, _text(attached)
            view = {"grant": meta["grant"], "attempt_id": meta["attempt_id"]}
            frame = await _until(app, view, "image")
            reply = _reply(frame)
            typed = await app.call_tool_mcp(
                "login_view_input",
                {
                    **view,
                    "page_id": reply["page_id"],
                    "geometry": reply["geometry"],
                    "events": [
                        {"type": "type", "text": "dä.test@example.com"},
                        {"type": "paste", "text": "pässwörd ✓ 🔑"},
                    ],
                },
            )
            (root / "finish").write_text("done")
            ended = await _until(app, view, "signed_in")

        # A fresh app process, after the login settled.
        async with Client(_server(root), init_timeout=90, timeout=60) as later:
            settled = await _frame(later, view)

    # The model's process has exited.
    async with Client(_server(root), init_timeout=90, timeout=60) as orphan:
        gone = await _frame(orphan, view)

    assert view["grant"] not in opened.model_dump_json()
    assert view["grant"] not in _text(attached)
    assert attached.structured_content is None
    assert reply["page_id"] == "p1" and reply["geometry"] == "800x600"
    assert len(_images(frame)) == 1
    assert _reply(typed) == {"access": "full", "input": "applied", "applied": 2}
    assert _events(root) == [
        ["type", "dä.test@example.com"],
        ["insert_text", "pässwörd ✓ 🔑"],
    ]
    assert _reply(ended)["access"] == "status" and _images(ended) == []
    assert _reply(settled)["state"] == "signed_in"
    assert _reply(gone) == {"access": "unavailable"}
