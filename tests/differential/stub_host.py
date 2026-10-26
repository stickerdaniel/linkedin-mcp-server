"""A host in a process of its own, which the harness kills whole (H-R4).

Started by ``harness.StubHost`` with the actors' environment and no
arguments, so its command line names no server module. It takes its orders on
stdin and reports on stdout, one JSON object a line:

* ``{"op": "start", "command": [...], "cwd": "..."}``: start the server as
  the harness's host stub does (``harness.HostQuitTransport``, under the same
  client and handshake) and initialize it; reported ``ready`` with the
  server's pid, or ``failed``.
* ``{"op": "call", "id": n, "tool": ..., "arguments": {...}}``: call the tool
  without waiting for it; reported ``returned`` with the call's summary
  (``harness.tool_summary``) or ``raised`` with the exception's class.
* ``{"op": "quit"}``, or the end of stdin: quit the server as a host does,
  stdin EOF and a wait, reported ``quit`` with how it ended.

The server's stderr goes to this process's stderr, a line at a time. Killed,
this process reports nothing: its server finds the three pipes it held
broken, and is sent nothing else.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# The harness's package, which running this file by path does not import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastmcp import Client  # noqa: E402

from differential.harness import (  # noqa: E402
    _CALL_SECONDS,
    _INIT_SECONDS,
    HostQuitTransport,
    tool_summary,
)


def say(**event: Any) -> None:
    sys.stdout.write(json.dumps(event, default=str) + "\n")
    sys.stdout.flush()


async def orders() -> dict[str, Any] | None:
    """The next order, or None at the end of stdin."""
    line = await asyncio.get_running_loop().run_in_executor(None, sys.stdin.readline)
    return json.loads(line) if line.strip() else None


async def serve() -> None:
    start = await orders()
    if start is None or start.get("op") != "start":
        say(event="failed", error=f"no start order: {start!r}")
        return
    transport = HostQuitTransport(
        start["command"],
        env=dict(os.environ),
        cwd=Path(start["cwd"]),
        on_stderr=lambda line: print(line, file=sys.stderr, flush=True),
    )
    client = Client(transport, init_timeout=_INIT_SECONDS, mode="legacy")
    calls: set[asyncio.Future[None]] = set()

    async def call(order: dict[str, Any]) -> None:
        try:
            called = await client.call_tool_mcp(
                order["tool"], order["arguments"], timeout=_CALL_SECONDS
            )
        except Exception as exc:  # noqa: BLE001 - reported to the harness
            say(
                event="raised",
                id=order["id"],
                exception=type(exc).__name__,
                error=str(exc)[:500],
            )
        else:
            say(event="returned", id=order["id"], summary=tool_summary(called))

    try:
        async with client:
            say(event="ready", server_pid=transport.pid)
            while True:
                order = await orders()
                if order is None or order.get("op") == "quit":
                    break
                if order.get("op") == "call":
                    running = asyncio.ensure_future(call(order))
                    calls.add(running)
                    running.add_done_callback(calls.discard)
            await transport.host_quit()
            say(
                event="quit",
                host={
                    "alive_before_quit": transport.alive_before_quit,
                    "stdin_closed": transport.stdin_closed,
                    "exited_on_quit": transport.exited_on_quit,
                    "exit_code": (
                        transport.process.returncode
                        if transport.process is not None
                        else None
                    ),
                    "quit_seconds": transport.quit_seconds,
                },
            )
    except Exception as exc:  # noqa: BLE001 - reported to the harness
        say(event="failed", error=f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(serve())
