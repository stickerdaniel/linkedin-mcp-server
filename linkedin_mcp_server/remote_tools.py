"""FastMCP side of the remote catalog: tools that run on the remote end.

``cli_main`` calls :func:`install` once, on a Direct server, after the server
is built and before it runs. It waits for the catalog that
``remote_catalog.prefetch`` started at most until the start deadline, checks
it, and only then adds every tool and the instructions line at once. Any
failure leaves the server exactly as it was built.

The middlewares ask :func:`is_remote` so that a remote tool is treated like
one that reads local state only: it never waits for the browser and never
takes the update notice meant for a LinkedIn tool.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from functools import partial
from typing import Any

import anyio
import anyio.lowlevel
import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool, ToolResult
from pydantic import Field

from linkedin_mcp_server import remote_catalog

logger = logging.getLogger(__name__)

#: The longest a remote tool call takes, from the call to its result.
CALL_BUDGET_SECONDS = 15.0

#: Calls whose request thread has not finished yet, at most four. A thread
#: blocked in name resolution outlives its caller's budget, so without this
#: bound a stalled resolver would collect one thread per call.
_OUTSTANDING = threading.BoundedSemaphore(4)

_installed: frozenset[str] = frozenset()
_attempted = False


def is_remote(name: str) -> bool:
    """Whether *name* is a tool this process loaded from the remote catalog."""
    return name in _installed


def _unavailable() -> ToolResult:
    return ToolResult(
        content=[mt.TextContent(type="text", text=remote_catalog.UNAVAILABLE_TEXT)],
        is_error=True,
    )


def _client_header() -> str | None:
    """``X-MCP-Client`` from the calling session's ``clientInfo``, if it sent one."""
    try:
        params = get_context().session.client_params
    except RuntimeError:
        return None
    info = params.client_info if params is not None else None
    if info is None:
        return None
    return remote_catalog.client_header(info.name, info.version)


def _transport() -> str:
    """The transport this process serves, as the configuration finally holds it."""
    from linkedin_mcp_server.config import get_config

    return get_config().server.transport


def _settle(
    future: asyncio.Future[remote_catalog.CallResult],
    outcome: remote_catalog.CallResult | Exception,
) -> None:
    """Resolve *future* on its loop, unless the caller stopped waiting for it."""
    if future.done():
        return
    if isinstance(outcome, Exception):
        future.set_exception(outcome)
    else:
        future.set_result(outcome)


async def _in_daemon_thread(
    request: Callable[[], remote_catalog.CallResult],
) -> remote_catalog.CallResult | None:
    """Run *request* in a thread of its own; ``None`` when four are still running.

    The thread is a daemon so that one blocked request, which nothing can
    interrupt, never holds the process open after the server has stopped. It
    hands its outcome back through ``call_soon_threadsafe``; once the caller
    was cancelled or the loop closed, the outcome is dropped. A caller that is
    already cancelled gets its cancellation before any request leaves.
    """
    await anyio.lowlevel.checkpoint_if_cancelled()
    if not _OUTSTANDING.acquire(blocking=False):
        return None
    loop = asyncio.get_running_loop()
    future: asyncio.Future[remote_catalog.CallResult] = loop.create_future()

    def work() -> None:
        outcome: remote_catalog.CallResult | Exception
        try:
            outcome = request()
        except Exception as exc:  # noqa: BLE001 - handed to the caller
            outcome = exc
        finally:
            _OUTSTANDING.release()
        try:
            loop.call_soon_threadsafe(_settle, future, outcome)
        except RuntimeError:  # the loop closed while the request ran
            pass

    try:
        threading.Thread(target=work, name="linkedin-remote-call", daemon=True).start()
    except RuntimeError:
        _OUTSTANDING.release()
        return None
    return await future


class RemoteTool(Tool):
    """A tool whose body is one POST to the catalog's call URL.

    Arguments go out unvalidated; the remote end checks them against the same
    schema and answers a 400 with ``isError``. Only the text items of its
    answer come back, rebuilt here, so nothing else it sends reaches the
    client.
    """

    call_url: str = Field(exclude=True)

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        deadline = time.monotonic() + CALL_BUDGET_SECONDS
        request = partial(
            remote_catalog.call,
            name=self.name,
            arguments=arguments,
            client=_client_header(),
            transport=_transport(),
            deadline=deadline,
            url=self.call_url,
        )
        try:
            # The thread bounds itself by the same deadline and sends nothing
            # once it has passed; leaving it here only stops waiting for it.
            with anyio.fail_after(CALL_BUDGET_SECONDS):
                result = await _in_daemon_thread(request)
        except (TimeoutError, remote_catalog.Unavailable):
            return _unavailable()
        if result is None:
            return _unavailable()
        return ToolResult(
            content=[mt.TextContent(type="text", text=text) for text in result.texts],
            is_error=result.is_error,
        )


def _joined_instructions(base: str | None, fragment: str) -> str | None:
    """*base*, a blank line, then *fragment*; *base* alone for an empty fragment."""
    if not fragment:
        return base
    return (base or "").rstrip("\n") + "\n\n" + fragment.strip() + "\n"


def install(mcp: FastMCP) -> None:
    """Add the remote catalog's tools and instructions line to *mcp*, or nothing.

    Blocking: waits for the catalog at most until the start deadline. Call it
    once per process, on a Direct server, before ``mcp.run``. Every tool and
    the new instructions are built before any of it is published, so a
    failure at any step registers nothing and logs one debug line.
    """
    global _attempted, _installed
    if _attempted:
        return
    _attempted = True
    try:
        body = remote_catalog.awaited_body()
        if body is None:
            if remote_catalog.requested():
                logger.debug("Optional tools were not loaded: no catalog in time")
            return
        builtin = frozenset(
            tool.name for tool in asyncio.run(mcp.list_tools(run_middleware=False))
        )
        catalog = remote_catalog.admit(body, builtin=builtin)
        url = remote_catalog.call_url()
        tools = [
            RemoteTool(
                name=spec.name,
                title=spec.title,
                description=spec.description,
                parameters=spec.input_schema,
                annotations=mt.ToolAnnotations.model_validate(spec.annotations)
                if spec.annotations
                else None,
                call_url=url,
            )
            for spec in catalog.tools
        ]
        instructions = _joined_instructions(mcp.instructions, catalog.instructions)
    except Exception as exc:  # noqa: BLE001 - the server runs on without them
        logger.debug("Optional tools were not loaded: %s", exc)
        return
    for tool in tools:
        mcp.add_tool(tool)
    mcp.instructions = instructions
    _installed = frozenset(tool.name for tool in tools)


def reset_for_testing() -> None:
    """Forget what this process installed, so the next :func:`install` runs."""
    global _attempted, _installed
    _attempted = False
    _installed = frozenset()
