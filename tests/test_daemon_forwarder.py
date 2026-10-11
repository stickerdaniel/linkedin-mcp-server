"""The frontend's forwarder: what a client reads through it, and what it may touch.

Every behaviour here is read through the production proxy server
(``create_mcp_server`` with the proxy role, its middleware and the forwarder),
reaching an in-process owner over the forwarder's real HTTP hop. Only the socket
is replaced (``owner_hop``). Fidelity is judged against the owner itself: what a
client reads through the frontend has to equal what the same client reads from
the owner directly.

The last class holds the line the forwarder's module docstring draws: no private
attribute, import or hook of FastMCP or the MCP SDK anywhere in the daemon
package, beyond the two provider hooks FastMCP documents for overriding. Both
libraries are resolved fresh by ``uvx`` on every install, and a private name is
the first thing a release moves.
"""

from __future__ import annotations

import ast
import base64
import importlib.util
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx2
import mcp.types as mt
import pytest
from fastmcp import Client, Context, FastMCP
from fastmcp.tools import ToolResult
from owner_hop import Forward, Interceptor, method_of, reach_owners_in_process
from test_daemon_proxy import _attachment, _backend

import linkedin_mcp_server.daemon as daemon_package
from linkedin_mcp_server.daemon.liveness import CALL_HEADER


@pytest.fixture(autouse=True)
def _no_update_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the update check off the network and out of the compared results."""
    import linkedin_mcp_server.update_check as update_check

    monkeypatch.setattr(update_check, "prime_from_cache", lambda: None)
    monkeypatch.setattr(update_check, "refresh_latest_version", AsyncMock())
    monkeypatch.setattr(update_check, "pending_update_notice", lambda: None)


def _proxy(tmp_path: Path, *, tool_timeout: float = 5.0) -> FastMCP:
    from linkedin_mcp_server.server import create_mcp_server
    from linkedin_mcp_server.server_role import ServerRole

    return create_mcp_server(
        role=ServerRole.PROXY,
        proxy_backend=_backend(_attachment(tmp_path), tmp_path),
        tool_timeout=tool_timeout,
    )


_PIXEL = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
_SOUND = base64.b64encode(b"RIFF\x00\x00\x00\x00WAVE").decode()

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "sections": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["url", "sections"],
}


def _owner_with_everything() -> FastMCP:
    """An owner whose one tool uses every field a listing and a result can carry."""
    owner = FastMCP("owner")

    @owner.tool(
        name="get_person_profile",
        title="Get Person Profile",
        description="Read a profile.",
        annotations=mt.ToolAnnotations(
            title="Profile reader",
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=True,
        ),
        output_schema=_OUTPUT_SCHEMA,
        icons=[mt.Icon(src="https://example.invalid/icon.png", mime_type="image/png")],
        meta={"owner": {"listed": True}},
        tags={"profile", "read"},
    )
    async def get_person_profile(
        linkedin_username: str, sections: list[str] | None = None
    ) -> ToolResult:
        """Read a profile."""
        return ToolResult(
            content=[
                mt.TextContent(
                    type="text",
                    text=f"profile of {linkedin_username}",
                    annotations=mt.Annotations(audience=["user"], priority=0.25),
                    meta={"block": "text"},
                ),
                mt.ImageContent(type="image", data=_PIXEL, mime_type="image/png"),
                mt.AudioContent(type="audio", data=_SOUND, mime_type="audio/wav"),
                mt.EmbeddedResource(
                    type="resource",
                    resource=mt.TextResourceContents(
                        uri="https://example.invalid/profile.txt",
                        mime_type="text/plain",
                        text="embedded",
                    ),
                    annotations=mt.Annotations(audience=["assistant"]),
                ),
                mt.ResourceLink(
                    type="resource_link",
                    name="profile",
                    title="The profile",
                    uri="https://example.invalid/in/someone",
                    description="Where it was read",
                    mime_type="text/html",
                    size=12,
                ),
            ],
            structured_content={
                "url": f"https://example.invalid/in/{linkedin_username}",
                "sections": {name: "text" for name in sections or ["main"]},
            },
            meta={"owner": {"answered": True}},
        )

    @owner.tool(name="refused", annotations=mt.ToolAnnotations(read_only_hint=True))
    async def refused() -> ToolResult:
        return ToolResult(
            content=[mt.TextContent(type="text", text="the owner said no")],
            structured_content={"why": "refused"},
            meta={"owner": {"refused": True}},
            is_error=True,
        )

    return owner


def _comparable(model: Any) -> Any:
    """*model* as a client reads it, less what names the server it came from.

    Each server stamps its own name on what it answers, so a frontend's result
    names the frontend; that key describes the connection, not the answer.
    """
    dumped = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    meta = dumped.get("_meta")
    if isinstance(meta, dict):
        meta.pop(mt.SERVER_INFO_META_KEY, None)
    return dumped


class TestWhatAClientReads:
    """The frontend serves the owner's tools and results exactly as the owner does."""

    async def test_a_result_carries_everything_the_owner_answered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _owner_with_everything()
        reach_owners_in_process(monkeypatch, lambda _url: owner)
        arguments = {"linkedin_username": "someone", "sections": ["main", "posts"]}

        async with Client(owner) as direct:
            expected = await direct.call_tool_mcp("get_person_profile", arguments)
        async with Client(_proxy(tmp_path)) as client:
            forwarded = await client.call_tool_mcp("get_person_profile", arguments)

        # Every content kind, its annotations and `_meta`, the structured half,
        # the result's own `_meta`, and the error flag.
        assert len(expected.content) == 5
        assert _comparable(forwarded) == _comparable(expected)

    async def test_an_error_result_stays_the_owners_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _owner_with_everything()
        reach_owners_in_process(monkeypatch, lambda _url: owner)

        async with Client(owner) as direct:
            expected = await direct.call_tool_mcp("refused", {})
        async with Client(_proxy(tmp_path)) as client:
            forwarded = await client.call_tool_mcp("refused", {})

        assert expected.is_error is True
        assert _comparable(forwarded) == _comparable(expected)

    async def test_a_listing_carries_everything_the_owner_lists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        owner = _owner_with_everything()
        reach_owners_in_process(monkeypatch, lambda _url: owner)

        async with Client(owner) as direct:
            expected = await direct.list_tools_mcp()
        async with Client(_proxy(tmp_path)) as client:
            forwarded = await client.list_tools_mcp()

        # Input and output schemas, title, annotations, icons and `_meta`.
        (profile,) = [t for t in expected.tools if t.name == "get_person_profile"]
        assert profile.output_schema is not None
        assert profile.icons
        assert _comparable(forwarded) == _comparable(expected)

    async def test_progress_and_logs_reach_the_caller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from fastmcp.client.logging import LogMessage

        owner = FastMCP("owner")

        @owner.tool(name="get_feed")
        async def get_feed(ctx: Context) -> str:
            await ctx.report_progress(progress=1, total=3, message="first")
            await ctx.info("reading", extra={"page": 1})
            await ctx.report_progress(progress=3, total=3, message="done")
            return "read"

        reach_owners_in_process(monkeypatch, lambda _url: owner)
        progress: list[tuple[float, float | None, str | None]] = []
        logs: list[tuple[str, Any]] = []

        async def on_progress(
            done: float, total: float | None, message: str | None
        ) -> None:
            progress.append((done, total, message))

        async def on_log(message: LogMessage) -> None:
            logs.append((message.level, message.data))

        async with Client(
            _proxy(tmp_path), progress_handler=on_progress, log_handler=on_log
        ) as client:
            result = await client.call_tool("get_feed", {})

        assert result.data == "read"
        assert progress == [(1.0, 3.0, "first"), (3.0, 3.0, "done")]
        assert [
            (level, data.get("msg"), data.get("extra"))
            for level, data in logs
            if isinstance(data, dict)
        ] == [("info", "reading", {"page": 1})]

    async def test_a_host_that_never_asks_for_log_levels_still_gets_the_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """A handshake-era host, which names no log level on its requests.

        The hop's era sends a request's logs only when that request asks for
        them, and this host's requests carry nothing to forward that would.
        """
        from fastmcp.client.transports import FastMCPTransport

        owner = FastMCP("owner")

        @owner.tool(name="get_feed")
        async def get_feed(ctx: Context) -> str:
            await ctx.warning("slow page", extra={"page": 2})
            return "read"

        reach_owners_in_process(monkeypatch, lambda _url: owner)
        logs: list[tuple[str, Any]] = []

        async def on_log(params: mt.LoggingMessageNotificationParams) -> None:
            logs.append((params.level, params.data))

        async with FastMCPTransport(_proxy(tmp_path)).connect_session(
            logging_callback=on_log, log_level=None
        ) as session:
            await session.initialize()
            result = await session.call_tool("get_feed", {})

        assert result.is_error is False
        assert [
            (level, data.get("msg"), data.get("extra"))
            for level, data in logs
            if isinstance(data, dict)
        ] == [("warning", "slow page", {"page": 2})]


class _RecordsHeaders(Interceptor):
    """Every request that reached the owner: its JSON-RPC method and headers."""

    def __init__(self, owner: FastMCP) -> None:
        super().__init__(owner)
        self.requests: list[tuple[str, str | None, httpx2.Headers]] = []

    async def handle(
        self, message: dict[str, Any] | None, request: httpx2.Request, forward: Forward
    ) -> httpx2.Response:
        self.requests.append((request.method, method_of(message), request.headers))
        return await forward()


class TestTheCallHeader:
    async def test_every_request_a_call_makes_names_that_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The owner ties heartbeats to a call by this header, on every request.

        A request without it is one the owner cannot cancel when the frontend
        goes away. Each call's hops, its lookup and its tool request alike,
        carry the call's own id, and two calls carry two.
        """
        owner = FastMCP("owner")

        @owner.tool(name="get_feed")
        async def get_feed() -> str:
            return "read"

        recorded = _RecordsHeaders(owner)
        reach_owners_in_process(monkeypatch, lambda _url: recorded)

        async with Client(_proxy(tmp_path)) as client:
            # Listed first, so the client's own check of each result's output
            # schema has what it needs and makes no listing of its own.
            await client.list_tools()
            recorded.requests.clear()
            await client.call_tool("get_feed", {})
            first = list(recorded.requests)
            recorded.requests.clear()
            await client.call_tool("get_feed", {})
            second = list(recorded.requests)

        ids = []
        for requests in (first, second):
            seen = [(verb, method) for verb, method, _ in requests]
            # Two hops, the lookup's and the call's, each with its own
            # negotiation.
            assert seen == [
                ("POST", "server/discover"),
                ("POST", "tools/list"),
                ("POST", "server/discover"),
                ("POST", "tools/call"),
            ], seen
            named = {headers.get(CALL_HEADER) for _, _, headers in requests}
            (call_id,) = named
            assert call_id, seen
            ids.append(call_id)
        assert ids[0] != ids[1]


class TestHowFarTheRequestGot:
    """The dispatch record a call's binding carries, read off the wire.

    Real-process counterparts: `TestAnOwnerProcessKilledBeforeTheCall` and
    `TestAnOwnerProcessKilledMidCall` (test_daemon_proxy.py), and the withdrawn
    queued call in `TestAForwardedCallThroughTheRealOwner`
    (test_daemon_election.py).
    """

    @staticmethod
    def _bound(tmp_path: Path) -> tuple[Any, Any]:
        from linkedin_mcp_server.daemon.forwarder import CallBinding, OwnerForwarder

        backend = _backend(_attachment(tmp_path), tmp_path)
        binding = CallBinding("call-1", backend.attachment_for_a_call())
        return OwnerForwarder(backend, timeout=5.0), binding

    async def test_a_refused_connection_sent_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import (
            OwnerFailure,
            OwnerUnreachableError,
            bound_to,
        )

        reach_owners_in_process(monkeypatch, lambda _url: None)
        forwarder, binding = self._bound(tmp_path)

        with bound_to(binding), pytest.raises(OwnerUnreachableError) as failed:
            await forwarder.call("send_message", {"message": "hi"})

        assert failed.value.nothing_was_sent is True
        assert failed.value.classification is OwnerFailure.UNREACHABLE
        assert binding.dispatch.state is Dispatch.UNSENT

    async def test_an_error_the_owner_sent_is_its_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The SDK raises the owner's JSON-RPC error out of the request, so only
        # the wire can say the owner answered it.
        from mcp import MCPError

        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import bound_to

        from test_daemon_proxy import _answers_with_an_error

        owner = _answers_with_an_error(
            FastMCP("owner"), code=-32001, message="refused here", fails="call"
        )

        @owner.tool(name="send_message")
        async def send_message(message: str) -> str:
            return message

        reach_owners_in_process(monkeypatch, lambda _url: owner)
        forwarder, binding = self._bound(tmp_path)

        with bound_to(binding), pytest.raises(MCPError, match="refused here"):
            await forwarder.call("send_message", {"message": "hi"})

        assert binding.dispatch.state is Dispatch.ANSWERED

    async def test_an_answer_cut_off_before_its_end_is_not_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The owner's whole answer is on the stream except the blank line that
        # ends its event, and then the stream ends. The SDK discards an
        # unterminated event, so the caller never got an answer.
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import (
            OwnerUnreachableError,
            bound_to,
        )

        class _CutOff(Interceptor):
            async def handle(
                self,
                message: dict[str, Any] | None,
                request: httpx2.Request,
                forward: Forward,
            ) -> httpx2.Response:
                if method_of(message) != "tools/call" or message is None:
                    return await forward()
                answer = {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "result": {"content": [], "isError": False},
                }

                class Unterminated(httpx2.AsyncByteStream):
                    async def __aiter__(self):
                        yield f"event: message\ndata: {json.dumps(answer)}\n".encode()

                return httpx2.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=Unterminated(),
                )

        owner = FastMCP("owner")

        @owner.tool(name="send_message")
        async def send_message(message: str) -> str:
            return message

        cut = _CutOff(owner)
        reach_owners_in_process(monkeypatch, lambda _url: cut)
        forwarder, binding = self._bound(tmp_path)

        with bound_to(binding), pytest.raises(OwnerUnreachableError) as failed:
            await forwarder.call("send_message", {"message": "hi"})

        assert failed.value.nothing_was_sent is False
        assert binding.dispatch.state is Dispatch.POSSIBLY_SENT

    def test_the_record_never_moves_back(self):
        from linkedin_mcp_server.daemon.core.call import Dispatch
        from linkedin_mcp_server.daemon.forwarder import DispatchRecord

        record = DispatchRecord()
        record.advance(Dispatch.ANSWERED)
        record.advance(Dispatch.POSSIBLY_SENT)
        record.advance(Dispatch.UNSENT)

        assert record.state is Dispatch.ANSWERED
        assert record.nothing_was_sent is False


# --- The line the forwarder's docstring draws -------------------------------

#: The provider hooks FastMCP documents for a provider to override.
_ALLOWED_OVERRIDES = frozenset({"_list_tools", "_get_tool"})

#: Libraries whose private names the daemon may not touch.
_GUARDED = ("fastmcp", "mcp")


def _private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _sources(package: str) -> Iterator[Path]:
    spec = importlib.util.find_spec(package)
    assert spec is not None and spec.submodule_search_locations, package
    for location in spec.submodule_search_locations:
        yield from sorted(Path(location).rglob("*.py"))


def _defined_private_names(paths: Iterator[Path]) -> set[str]:
    """Every private name a body of code defines: functions, classes, attributes."""
    names: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                candidates = [node.name]
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                candidates = [node.attr]
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                candidates = [node.id]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                candidates = [node.target.id]
            else:
                continue
            names.update(name for name in candidates if _private(name))
    return names


def _library_private_names() -> set[str]:
    names: set[str] = set()
    for package in _GUARDED:
        names |= _defined_private_names(_sources(package))
    return names


def _daemon_sources() -> list[Path]:
    return sorted(Path(daemon_package.__file__).parent.rglob("*.py"))


def _guarded_module(module: str | None) -> bool:
    return module is not None and module.split(".")[0] in _GUARDED


def _private_access(
    path: Path, library: set[str], own: set[str]
) -> Iterator[tuple[str, int, str]]:
    """Every reach into a private name of FastMCP or the SDK in one daemon module.

    Imports are judged by where they come from. An attribute is judged by its
    name: one the libraries define and the daemon package does not. A method a
    class defines is judged by its base: a FastMCP or SDK class, or a class in
    the same module that has one, may override none of the libraries' private
    names but the documented hooks.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    here = path.name
    from_library: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and _guarded_module(node.module):
            module = node.module or ""
            if any(_private(part) for part in module.split(".")):
                yield here, node.lineno, f"from {module} import ..."
            for alias in node.names:
                if _private(alias.name):
                    yield here, node.lineno, f"from {module} import {alias.name}"
                from_library.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _guarded_module(alias.name):
                    if any(_private(part) for part in alias.name.split(".")):
                        yield here, node.lineno, f"import {alias.name}"
                    from_library.add(alias.asname or alias.name.split(".")[0])

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _private(node.attr):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            on_a_library_module = isinstance(root, ast.Name) and root.id in (
                from_library
            )
            if on_a_library_module or (node.attr in library and node.attr not in own):
                yield here, node.lineno, f".{node.attr}"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "setattr", "hasattr", "delattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
            and _private(node.args[1].value)
            and node.args[1].value in library
        ):
            yield here, node.lineno, f"{node.func.id}(..., {node.args[1].value!r})"

    library_classes: set[str] = set()
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    # Repeated until stable, so a subclass of a subclass is found in any order.
    while True:
        before = len(library_classes)
        for cls in classes:
            for base in cls.bases:
                root = base
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name) and (
                    root.id in from_library or root.id in library_classes
                ):
                    library_classes.add(cls.name)
        if len(library_classes) == before:
            break
    for cls in classes:
        if cls.name not in library_classes:
            continue
        for item in cls.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _private(item.name)
                and item.name in library
                and item.name not in _ALLOWED_OVERRIDES
            ):
                yield here, item.lineno, f"{cls.name}.{item.name} overrides"


class TestNoPrivateAccess:
    """The daemon reaches FastMCP and the SDK through public names only."""

    def test_the_daemon_package_uses_no_private_name_of_either_library(self):
        library = _library_private_names()
        sources = _daemon_sources()
        own = _defined_private_names(iter(sources)) - _ALLOWED_OVERRIDES
        # Sanity: the scan sees the libraries' private names at all.
        assert {"_list_tools", "_get_tool"} <= library

        found = [
            finding
            for path in sources
            for finding in _private_access(path, library, own)
        ]

        assert found == []

    def test_the_guard_finds_each_kind_of_reach(self, tmp_path: Path):
        # The scan above passes on an empty list, so what it would catch is
        # shown on a module that does each thing once.
        planted = tmp_path / "planted.py"
        planted.write_text(
            "from fastmcp.server.providers import Provider\n"
            "from mcp.client import _private_helper\n"
            "from mcp.shared._private import thing\n"
            "import mcp.types as mt\n"
            "\n"
            "class Mine(Provider):\n"
            "    async def _list_tools(self): ...\n"
            "    async def _list_prompts(self): ...\n"
            "\n"
            "class Deeper(Mine):\n"
            "    async def _get_tool(self, name, version=None): ...\n"
            "    async def _list_resources(self): ...\n"
            "\n"
            "def reach(session):\n"
            "    session._write_stream\n"
            "    mt._anything\n"
            "    getattr(session, '_write_stream')\n",
            encoding="utf-8",
        )
        library = _library_private_names()

        found = sorted(
            what for _, _, what in _private_access(planted, library, own=set())
        )

        assert found == sorted(
            [
                "from mcp.client import _private_helper",
                "from mcp.shared._private import ...",
                "Mine._list_prompts overrides",
                "Deeper._list_resources overrides",
                "._write_stream",
                "._anything",
                "getattr(..., '_write_stream')",
            ]
        )
