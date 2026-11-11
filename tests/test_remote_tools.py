"""Optional tools from the remote catalog, as an MCP client sees them.

The catalog and its call endpoint are a local HTTP server (`catalog_stub`).
The start cases launch the real server as a process, through the installed
console script and through ``python -m``, and talk to it over stdio; the rest
build the production server in process and use a real FastMCP client.

Whatever goes wrong with the catalog, a client must see exactly the built-in
tools (`fixtures/tool-contract/tools.json`) and the built-in instructions.

The stub signs with a test key. In process, the ``stub`` fixture admits that
key; a started process gets it from a ``sitecustomize`` on its ``PYTHONPATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import string
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import urlsplit

import anyio
import mcp.types as mt
import pytest
from catalog_stub import (
    PUBLIC_KEY,
    SIGNATURE_HEADER,
    Answer,
    CatalogStub,
    gate_environment,
    signature,
)
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StdioTransport
from test_profile_lease_integration import _hold_profile
from test_server import (
    _THE_OPERATING_RULES,
    _TOOL_CONTRACT,
    _stand_in_the_lifespan,
    _wire_tools,
)

from linkedin_mcp_server import remote_catalog, remote_tools
from linkedin_mcp_server.server import create_mcp_server

_URL = remote_catalog.URL_ENV
_NAME = "sample_remote_tool"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "network": {"type": "string", "enum": ["one", "two"], "description": "N"},
        "at": {"type": "string", "maxLength": 40},
        "count": {"type": "integer", "minimum": 1, "maximum": 9, "default": 1},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["network"],
    "additionalProperties": False,
}

#: A catalog entry as the remote end sends it, with fields outside the
#: contract that must be dropped.
_ENTRY: dict[str, Any] = {
    "name": _NAME,
    "title": "Sample Remote Tool",
    "description": "Answers with a fixed text.",
    "inputSchema": _SCHEMA,
    "annotations": {"readOnlyHint": True, "openWorldHint": False, "x-other": 1},
    "outputSchema": {"type": "object"},
    "_meta": {"tracking": "t"},
}

#: The same tool as `tools/list` puts it on the wire.
_WIRE = {
    "name": _NAME,
    "title": "Sample Remote Tool",
    "description": "Answers with a fixed text.",
    "inputSchema": _SCHEMA,
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
    "_meta": {"fastmcp": {"tags": []}},
}

_CATALOG = {
    "instructions": "  Use sample_remote_tool when asked.\n\n",
    "tools": [_ENTRY],
}
#: The built-in rules, a blank line, then the fragment, stripped.
_JOINED = _THE_OPERATING_RULES + "\nUse sample_remote_tool when asked.\n"


def _builtin_contract() -> list[dict[str, Any]]:
    return json.loads(_TOOL_CONTRACT.read_text(encoding="utf-8"))


def _with_remote() -> list[dict[str, Any]]:
    return sorted([*_builtin_contract(), _WIRE], key=lambda tool: tool["name"])


def _entry(**changes: Any) -> dict[str, Any]:
    return {**_ENTRY, **changes}


def _schema(**changes: Any) -> dict[str, Any]:
    return {**_SCHEMA, **changes}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[CatalogStub]:
    monkeypatch.setattr(remote_catalog, "CATALOG_KEYS", (PUBLIC_KEY,))
    with gate_environment(monkeypatch, tmp_path / ".env"), CatalogStub() as server:
        os.environ[_URL] = server.url
        server.answer = server.json(200, _CATALOG)
        yield server


@pytest.fixture
def lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    _stand_in_the_lifespan(monkeypatch)


async def _started() -> FastMCP:
    """The Direct server as ``main()`` builds it: request first, install before run."""
    remote_catalog.prefetch([])
    mcp = create_mcp_server()
    # `install` blocks and runs its own event loop, as it does before `run`.
    await asyncio.to_thread(remote_tools.install, mcp)
    return mcp


async def _surface(
    mcp: FastMCP, mode: str = "auto"
) -> tuple[list[dict[str, Any]], str | None]:
    protocol = "2026-07-28" if mode == "auto" else "2025-11-25"
    tools = await _wire_tools(mcp, mode, protocol)
    async with Client(mcp, mode=mode) as client:
        return tools, client.instructions


# --------------------------------------------------------------------------
# Start, as a real process
# --------------------------------------------------------------------------

_CONSOLE_SCRIPT = Path(sys.executable).with_name("mcp-server-linkedin")


def _test_key_site(tmp_path: Path) -> Path:
    """A directory whose ``sitecustomize`` admits the stub's key at interpreter start."""
    site = tmp_path / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(
        "from linkedin_mcp_server import remote_catalog\n"
        f"remote_catalog.CATALOG_KEYS = (bytes.fromhex({PUBLIC_KEY.hex()!r}),)\n",
        encoding="utf-8",
    )
    return site


def _process_env(tmp_path: Path, **settings: str) -> dict[str, str]:
    """The environment a client gives the server, with a private profile.

    ``CHROME_PATH`` names an existing file so no browser is installed at start.
    ``PYTHONPATH`` leads with the test key's ``sitecustomize``.
    """
    env = {key: value for key, value in os.environ.items() if key not in _GATE_KEYS}
    python_path = [str(_test_key_site(tmp_path))]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env.update(
        HOME=str(tmp_path / "home"),
        USER_DATA_DIR=str(tmp_path / "auth" / "profile"),
        CHROME_PATH=sys.executable,
        PYTHONPATH=os.pathsep.join(python_path),
        **settings,
    )
    return env


_GATE_KEYS = frozenset(
    {
        _URL,
        "LINKEDIN_MCP_REMOTE_TOOLS",
        "LINKEDIN_MCP_CHECK_FOR_UPDATES",
        "CI",
        "DAEMON_ENABLED",
        "IMPORT_FROM_BROWSER",
        "LINKEDIN_MCP_RUNTIME",
        "USER_DATA_DIR",
    }
)


async def _start_process(
    command: list[str], env: dict[str, str], cwd: Path
) -> tuple[float, list[dict[str, Any]], str | None]:
    """Spawn, initialize and list; the seconds until the client was connected."""
    transport = StdioTransport(
        command=command[0], args=command[1:], env=env, cwd=str(cwd), keep_alive=False
    )
    started = time.monotonic()
    async with Client(transport, init_timeout=60) as client:
        connected = time.monotonic() - started
        tools = await client.list_tools()
        instructions = client.instructions
    wire = sorted(
        (
            tool.model_dump(mode="json", by_alias=True, exclude_none=True)
            for tool in tools
        ),
        key=lambda tool: tool["name"],
    )
    return connected, wire, instructions


@pytest.mark.slow
class TestStartedProcess:
    @pytest.mark.parametrize(
        "command",
        [[str(_CONSOLE_SCRIPT)], [sys.executable, "-m", "linkedin_mcp_server"]],
        ids=["console script", "python -m"],
    )
    async def test_a_served_catalog_reaches_the_client(
        self, stub: CatalogStub, tmp_path: Path, command: list[str]
    ):
        env = _process_env(tmp_path, **{_URL: stub.url})

        _, tools, instructions = await _start_process(command, env, tmp_path)

        assert tools == _with_remote()
        assert instructions == _JOINED

    async def test_an_unsigned_catalog_leaves_the_built_in_surface(
        self, stub: CatalogStub, tmp_path: Path
    ):
        stub.answer = stub.raw(200, json.dumps(_CATALOG).encode(), signed=False)

        _, tools, instructions = await _start_process(
            [str(_CONSOLE_SCRIPT)], _process_env(tmp_path, **{_URL: stub.url}), tmp_path
        )

        assert len(stub.requests) == 1
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_a_hanging_catalog_costs_at_most_the_budget(
        self, stub: CatalogStub, tmp_path: Path
    ):
        command = [str(_CONSOLE_SCRIPT)]
        off, base_tools, base_instructions = await _start_process(
            command,
            _process_env(tmp_path, LINKEDIN_MCP_REMOTE_TOOLS="off", **{_URL: stub.url}),
            tmp_path,
        )
        assert stub.requests == []
        stub.answer = stub.drip(every=0.5, headers_first=False)

        hanging, tools, instructions = await _start_process(
            command, _process_env(tmp_path, **{_URL: stub.url}), tmp_path
        )

        assert len(stub.requests) == 1
        assert (tools, instructions) == (base_tools, base_instructions)
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES
        assert hanging - off <= 2.5, (hanging, off)

    async def test_a_catalog_the_transport_cannot_encode_leaves_the_built_in_surface(
        self, stub: CatalogStub, tmp_path: Path
    ):
        # A lone surrogate is valid JSON and a valid Python string, but stdio
        # cannot encode it: admitted, it would fail the initialize answer.
        stub.answer = stub.json(200, {"instructions": "\ud800", "tools": [_ENTRY]})

        _, tools, instructions = await _start_process(
            [str(_CONSOLE_SCRIPT)], _process_env(tmp_path, **{_URL: stub.url}), tmp_path
        )

        assert len(stub.requests) == 1
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_a_malformed_url_override_still_starts_the_server(
        self, tmp_path: Path
    ):
        _, tools, instructions = await _start_process(
            [str(_CONSOLE_SCRIPT)],
            _process_env(tmp_path, **{_URL: "http://["}),
            tmp_path,
        )

        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES


# --------------------------------------------------------------------------
# Install, in process
# --------------------------------------------------------------------------


def _closed_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/oss/tools"


_SECOND = _entry(name="second_remote_tool")

_BROKEN = {
    "500": CatalogStub.json(500, _CATALOG),
    "invalid JSON": CatalogStub.raw(200, b'{"tools": ['),
    "not an object": CatalogStub.json(200, [_ENTRY]),
    "oversized": CatalogStub.raw(
        200, json.dumps(_CATALOG).encode() + b" " * (64 * 1024)
    ),
    "a bad name": CatalogStub.json(200, {"tools": [_ENTRY, _entry(name="bad name")]}),
    "a duplicate": CatalogStub.json(200, {"tools": [_ENTRY, _ENTRY]}),
    "a built-in name": CatalogStub.json(
        200, {"tools": [_ENTRY, _entry(name="get_session_status")]}
    ),
    "$ref": CatalogStub.json(
        200,
        {
            "tools": [
                _ENTRY,
                _SECOND
                | {
                    "inputSchema": _schema(
                        properties={"a": {"$ref": "#/x"}}, required=[]
                    )
                },
            ]
        },
    ),
    "a nested object": CatalogStub.json(
        200,
        {
            "tools": [
                _ENTRY,
                _SECOND
                | {
                    "inputSchema": _schema(
                        properties={"a": {"type": "object"}}, required=[]
                    )
                },
            ]
        },
    ),
    "required: 1": CatalogStub.json(
        200, {"tools": [_ENTRY, _SECOND | {"inputSchema": _schema(required=1)}]}
    ),
    "a long description": CatalogStub.json(
        200, {"tools": [_ENTRY, _SECOND | {"description": "x" * 4097}]}
    ),
    "a long instructions line": CatalogStub.json(
        200, {"instructions": "x" * 1001, "tools": [_ENTRY]}
    ),
    "a string hint": CatalogStub.json(
        200, {"tools": [_ENTRY, _SECOND | {"annotations": {"readOnlyHint": "yes"}}]}
    ),
    # `json.dumps` writes a lone surrogate as the escape `\ud800`.
    "a lone surrogate in the instructions": CatalogStub.json(
        200, {"instructions": "\ud800", "tools": [_ENTRY]}
    ),
    "a lone surrogate in a description": CatalogStub.json(
        200, {"tools": [_ENTRY, _SECOND | {"description": "x\udfff"}]}
    ),
    "a lone surrogate in a schema default": CatalogStub.json(
        200,
        {
            "tools": [
                _ENTRY,
                _SECOND
                | {
                    "inputSchema": _schema(
                        properties={"a": {"type": "string", "default": "\ud800"}},
                        required=[],
                    )
                },
            ]
        },
    ),
    # `json.dumps` writes NaN as the bare literal `NaN`.
    "minimum: NaN": CatalogStub.json(
        200,
        {
            "tools": [
                _ENTRY,
                _SECOND
                | {
                    "inputSchema": _schema(
                        properties={"a": {"type": "number", "minimum": float("nan")}},
                        required=[],
                    )
                },
            ]
        },
    ),
    "maximum: 1e999": CatalogStub.raw(
        200,
        json.dumps({"tools": [_ENTRY, _SECOND | {"inputSchema": "BOUND"}]})
        .replace(
            '"BOUND"',
            '{"type": "object", "properties": {"a": {"type": "number", '
            '"maximum": 1e999}}, "required": [], "additionalProperties": false}',
        )
        .encode(),
    ),
}


_BODY = json.dumps(_CATALOG).encode()
_SIGNATURE = signature(_BODY)
_BASE64URL = string.ascii_uppercase + string.ascii_lowercase + string.digits + "-_"
#: The last of 86 characters holds four unused bits, zero when canonical. This
#: sets one: it decodes to the same 64 bytes.
_NON_CANONICAL = _SIGNATURE[:-1] + _BASE64URL[_BASE64URL.index(_SIGNATURE[-1]) | 1]


def _with_signature(*values: str, body: bytes = _BODY) -> Answer:
    return CatalogStub.raw(
        200, body, [(SIGNATURE_HEADER, value) for value in values], signed=False
    )


#: Catalogs that pass every content check and fail only on their signature.
_FORGED = {
    "unsigned": CatalogStub.raw(200, _BODY, signed=False),
    "another key": _with_signature(signature(_BODY, key=Ed25519PrivateKey.generate())),
    "body altered after signing": _with_signature(
        _SIGNATURE, body=_BODY.replace(b"fixed text", b"other text")
    ),
    "signed without the prefix": _with_signature(signature(_BODY, prefix=b"")),
    "padded": _with_signature(_SIGNATURE + "=="),
    "standard base64": _with_signature(_SIGNATURE[:-2] + "+/"),
    "one character short": _with_signature(_SIGNATURE[:-1]),
    "one character long": _with_signature(_SIGNATURE + "A"),
    "non-canonical last character": _with_signature(_NON_CANONICAL),
    "comma-joined": _with_signature(f"{_SIGNATURE}, {_SIGNATURE}"),
    "duplicate header": _with_signature(_SIGNATURE, _SIGNATURE),
}


class TestSignature:
    @pytest.mark.parametrize("case", list(_FORGED))
    async def test_a_catalog_without_a_valid_signature_leaves_the_built_in_surface(
        self, stub: CatalogStub, lifespan: None, case: str
    ):
        stub.answer = _FORGED[case]

        tools, instructions = await _surface(await _started())

        assert len(stub.requests) == 1
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_any_listed_key_admits_the_catalog(
        self, stub: CatalogStub, lifespan: None, monkeypatch: pytest.MonkeyPatch
    ):
        other = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        monkeypatch.setattr(remote_catalog, "CATALOG_KEYS", (other, PUBLIC_KEY))

        tools, instructions = await _surface(await _started())

        assert tools == _with_remote()
        assert instructions == _JOINED


class TestInstall:
    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_a_served_catalog_adds_its_tools_and_instructions(
        self, stub: CatalogStub, lifespan: None, mode: str
    ):
        mcp = await _started()

        tools, instructions = await _surface(mcp, mode)

        assert tools == _with_remote()
        assert instructions == _JOINED

    async def test_an_empty_fragment_keeps_the_instructions(
        self, stub: CatalogStub, lifespan: None
    ):
        stub.answer = stub.json(200, {"tools": [_ENTRY]})

        tools, instructions = await _surface(await _started())

        assert tools == _with_remote()
        assert instructions == _THE_OPERATING_RULES

    async def test_the_catalog_decides_how_many_tools_it_serves(
        self, stub: CatalogStub, lifespan: None
    ):
        names = [f"remote_{index}" for index in range(6)]
        stub.answer = stub.json(200, {"tools": [_entry(name=name) for name in names]})

        tools, _ = await _surface(await _started())

        listed = {tool["name"] for tool in tools}
        assert listed - {tool["name"] for tool in _builtin_contract()} == set(names)

    @pytest.mark.parametrize("case", list(_BROKEN))
    async def test_a_broken_catalog_leaves_the_built_in_surface(
        self, stub: CatalogStub, lifespan: None, case: str
    ):
        stub.answer = _BROKEN[case]

        tools, instructions = await _surface(await _started())

        assert len(stub.requests) == 1
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_a_refused_connection_leaves_the_built_in_surface(
        self, stub: CatalogStub, lifespan: None
    ):
        os.environ[_URL] = _closed_port_url()

        tools, instructions = await _surface(await _started())

        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_a_redirect_leaves_the_built_in_surface(
        self, stub: CatalogStub, lifespan: None
    ):
        with CatalogStub() as elsewhere:
            elsewhere.answer = elsewhere.json(200, _CATALOG)
            stub.answer = stub.redirect(elsewhere.url)

            tools, instructions = await _surface(await _started())

            assert elsewhere.requests == []
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    async def test_a_catalog_completed_after_the_deadline_is_not_admitted(
        self, stub: CatalogStub, lifespan: None
    ):
        body = json.dumps(_CATALOG).encode()
        # Four parts 0.6 s apart: no gap reaches the 2.0 s socket timeout, and
        # the last part lands at 2.4 s, after the 2.0 s start deadline.
        stub.answer = stub.drip(_CATALOG, every=0.6, size=-(-len(body) // 4))

        remote_catalog.prefetch([])
        fetch = remote_catalog._pending
        assert fetch is not None
        assert await asyncio.to_thread(fetch.done.wait, 10)
        # The body did arrive complete; only its timing keeps it out.
        assert stub.finished.is_set()
        assert fetch.completed_at is not None
        mcp = create_mcp_server()
        await asyncio.to_thread(remote_tools.install, mcp)

        tools, instructions = await _surface(mcp)
        assert tools == _builtin_contract()
        assert instructions == _THE_OPERATING_RULES

    def test_install_returns_by_the_deadline_while_the_catalog_hangs(
        self, stub: CatalogStub
    ):
        stub.answer = stub.drip(every=0.5, headers_first=False)
        started = time.monotonic()

        remote_catalog.prefetch([])
        mcp = create_mcp_server()
        remote_tools.install(mcp)

        assert time.monotonic() - started <= 2.2
        assert len(stub.requests) == 1
        assert mcp.instructions == _THE_OPERATING_RULES
        assert not remote_tools.is_remote(_NAME)


# --------------------------------------------------------------------------
# Calls
# --------------------------------------------------------------------------

_OK = {"content": [{"type": "text", "text": "Fixed text."}], "isError": False}
_CLIENT = mt.Implementation(name="test-client", version="1.2.3")


def _texts(result: mt.CallToolResult) -> list[dict[str, Any]]:
    return [part.model_dump(mode="json", exclude_none=True) for part in result.content]


_UNAVAILABLE = [{"type": "text", "text": "This tool is unavailable right now."}]


class TestCalls:
    @pytest.mark.parametrize("mode", ["legacy", "auto"])
    async def test_a_call_sends_only_name_arguments_and_who_asks(
        self, stub: CatalogStub, lifespan: None, mode: str
    ):
        stub.call_answer = stub.json(200, _OK)
        mcp = await _started()
        arguments = {"network": "one", "tags": ["a"]}

        async with Client(mcp, mode=mode, client_info=_CLIENT) as client:
            result = await client.call_tool(
                _NAME, arguments, meta={"secret": "do-not-forward"}
            )

        assert (_texts(result), result.is_error) == (_OK["content"], False)
        [seen] = stub.calls()
        assert (seen.method, seen.path) == ("POST", "/oss/tools/call")
        assert seen.json() == {"name": _NAME, "arguments": arguments}
        assert seen.headers["x-mcp-client"] == "test-client/1.2.3"
        assert seen.headers["user-agent"].endswith("; stdio)")
        assert "do-not-forward" not in repr(seen)

    async def test_a_400_passes_its_text_and_error_flag_through(
        self, stub: CatalogStub, lifespan: None
    ):
        answer = {
            "content": [{"type": "text", "text": "network: not one of"}],
            "isError": True,
        }
        stub.call_answer = stub.json(400, answer)
        mcp = await _started()

        async with Client(mcp) as client:
            result = await client.call_tool(_NAME, {"network": 3}, raise_on_error=False)

        assert (_texts(result), result.is_error) == (answer["content"], True)

    async def test_only_the_text_items_reach_the_client(
        self, stub: CatalogStub, lifespan: None
    ):
        stub.call_answer = stub.json(
            200,
            {
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "image", "data": "aGk=", "mimeType": "image/png"},
                    {
                        "type": "text",
                        "text": "two",
                        "annotations": {"audience": ["user"]},
                        "_meta": {"tracking": "item"},
                    },
                ],
                "structuredContent": {"tracking": "structured"},
                "_meta": {"tracking": "result"},
                "isError": False,
            },
        )
        mcp = await _started()

        async with Client(mcp) as client:
            result = await client.call_tool(_NAME, {"network": "one"})

        assert _texts(result) == [
            {"type": "text", "text": "one"},
            {"type": "text", "text": "two"},
        ]
        assert result.structured_content is None
        # Nothing the remote attached beyond its text reaches the client.
        assert "tracking" not in repr(result)

    @pytest.mark.parametrize(
        "answer",
        [
            CatalogStub.json(500, _OK),
            CatalogStub.raw(200, b"not json"),
            CatalogStub.json(200, {"content": "text"}),
            CatalogStub.raw(200, b" " * (256 * 1024 + 1)),
            CatalogStub.json(200, {"content": [{"type": "text", "text": "\ud800"}]}),
        ],
        ids=["500", "invalid JSON", "wrong shape", "oversized", "a lone surrogate"],
    )
    async def test_a_bad_answer_is_the_fixed_error(
        self, stub: CatalogStub, lifespan: None, answer: Any
    ):
        stub.call_answer = answer
        mcp = await _started()

        async with Client(mcp) as client:
            result = await client.call_tool(
                _NAME, {"network": "one"}, raise_on_error=False
            )

        assert (_texts(result), result.is_error) == (_UNAVAILABLE, True)

    async def test_a_long_text_within_the_answer_limit_reaches_the_client(
        self, stub: CatalogStub, lifespan: None
    ):
        text = "x" * (64 * 1024 + 1)
        stub.call_answer = stub.json(
            200, {"content": [{"type": "text", "text": text}], "isError": False}
        )
        mcp = await _started()

        async with Client(mcp) as client:
            result = await client.call_tool(_NAME, {"network": "one"})

        assert _texts(result) == [{"type": "text", "text": text}]

    async def test_a_redirected_call_is_not_followed(
        self, stub: CatalogStub, lifespan: None
    ):
        with CatalogStub() as elsewhere:
            elsewhere.answer = elsewhere.json(200, _OK)
            stub.call_answer = stub.redirect(elsewhere.url + "/call")
            mcp = await _started()

            async with Client(mcp) as client:
                result = await client.call_tool(
                    _NAME, {"network": "one"}, raise_on_error=False
                )

            assert elsewhere.requests == []
        assert (_texts(result), result.is_error) == (_UNAVAILABLE, True)


_BUDGET = 1.0
#: A host name only the tests below resolve, slowly.
_STALLED_HOST = "resolver-stall.invalid"


@pytest.fixture
def short_budget(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(remote_tools, "CALL_BUDGET_SECONDS", _BUDGET)
    return _BUDGET


class TestCallBudget:
    @pytest.mark.parametrize(
        "never_done",
        [
            {"headers_first": False},
            {"headers_first": True},
            {"document": {"content": [{"type": "text", "text": "late" * 10}]}},
        ],
        ids=["no status line", "endless body", "complete only after the budget"],
    )
    async def test_a_slow_remote_is_the_fixed_error_within_the_budget(
        self,
        stub: CatalogStub,
        lifespan: None,
        short_budget: float,
        never_done: dict[str, Any],
    ):
        stub.call_answer = stub.drip(every=0.05, **never_done)
        mcp = await _started()

        async with Client(mcp) as client:
            started = time.monotonic()
            result = await client.call_tool(
                _NAME, {"network": "one"}, raise_on_error=False
            )
            elapsed = time.monotonic() - started
            # The request thread stops reading at the deadline too, rather
            # than being left to drain the answer.
            for _ in range(40):
                if stub.disconnected_at is not None:
                    break
                await asyncio.sleep(0.05)

        assert (_texts(result), result.is_error) == (_UNAVAILABLE, True)
        assert elapsed <= short_budget + 0.5
        assert stub.disconnected_at is not None
        assert stub.disconnected_at - started <= short_budget + 0.5

    async def test_a_fifth_call_while_four_are_pending_is_the_fixed_error_at_once(
        self, stub: CatalogStub, lifespan: None, short_budget: float
    ):
        stub.call_answer = stub.drip(every=0.05)
        mcp = await _started()
        arguments = {"network": "one"}

        async with Client(mcp) as client:
            pending = [
                asyncio.create_task(
                    client.call_tool(_NAME, arguments, raise_on_error=False)
                )
                for _ in range(4)
            ]
            with anyio.fail_after(5):
                while len(stub.calls()) < 4:
                    await asyncio.sleep(0.02)
            started = time.monotonic()
            fifth = await client.call_tool(_NAME, arguments, raise_on_error=False)
            elapsed = time.monotonic() - started
            sent_while_full = len(stub.calls())
            await asyncio.gather(*pending)
            # Each request ends at its own deadline and frees its place.
            stub.call_answer = stub.json(200, _OK)
            with anyio.fail_after(3):
                while True:
                    again = await client.call_tool(
                        _NAME, arguments, raise_on_error=False
                    )
                    if not again.is_error:
                        break
                    await asyncio.sleep(0.05)

        assert (_texts(fifth), fifth.is_error) == (_UNAVAILABLE, True)
        assert elapsed < 0.5
        assert sent_while_full == 4
        assert _texts(again) == _OK["content"]

    async def test_a_budget_spent_resolving_the_host_sends_nothing(
        self, stub: CatalogStub, short_budget: float, monkeypatch: pytest.MonkeyPatch
    ):
        resolve = socket.getaddrinfo
        resolved = threading.Event()

        def slow_resolve(host: Any, *args: Any, **kwargs: Any) -> Any:
            if host != _STALLED_HOST:
                return resolve(host, *args, **kwargs)
            time.sleep(short_budget + 1.0)
            try:
                return resolve("127.0.0.1", *args, **kwargs)
            finally:
                resolved.set()

        monkeypatch.setattr(socket, "getaddrinfo", slow_resolve)
        tool = remote_tools.RemoteTool(
            name=_NAME,
            description="",
            parameters=_SCHEMA,
            call_url=f"http://{_STALLED_HOST}:{urlsplit(stub.url).port}/oss/tools/call",
        )

        result = await tool.run({"network": "one"})
        assert not resolved.is_set()
        assert await asyncio.to_thread(resolved.wait, 5)
        # Time for a request sent after resolution to reach the stub.
        await asyncio.sleep(0.5)

        texts = [
            part.model_dump(mode="json", exclude_none=True) for part in result.content
        ]
        assert (texts, result.is_error) == (_UNAVAILABLE, True)
        assert stub.calls() == []

    def test_a_call_blocked_resolving_the_host_does_not_hold_the_process_open(
        self, tmp_path: Path
    ):
        # The child stalls resolution for 5 s, lets the 0.2 s budget pass,
        # leaves its event loop and should exit at once, not 5 s later.
        child = textwrap.dedent(
            f"""
            import asyncio, socket, time
            from linkedin_mcp_server import remote_tools

            resolve = socket.getaddrinfo

            def slow_resolve(host, *args, **kwargs):
                if host == {_STALLED_HOST!r}:
                    time.sleep(5)
                    host = "127.0.0.1"
                return resolve(host, *args, **kwargs)

            socket.getaddrinfo = slow_resolve
            remote_tools.CALL_BUDGET_SECONDS = 0.2
            tool = remote_tools.RemoteTool(
                name="blocked",
                description="",
                parameters={_SCHEMA!r},
                call_url="http://{_STALLED_HOST}:9/oss/tools/call",
            )
            result = asyncio.run(tool.run({{"network": "one"}}))
            print("returned", result.is_error, flush=True)
            """
        )
        env = {key: value for key, value in os.environ.items() if key not in _GATE_KEYS}
        env.update(USER_DATA_DIR=str(tmp_path / "auth" / "profile"), NO_PROXY="*")
        with subprocess.Popen(
            [sys.executable, "-c", child],
            env=env,
            cwd=tmp_path,
            stdout=subprocess.PIPE,
            text=True,
        ) as process:
            assert process.stdout is not None
            line = process.stdout.readline()
            returned = time.monotonic()
            try:
                code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                raise
            exited = time.monotonic()

        assert line == "returned True\n"
        assert code == 0
        # A held process exits about 4.8 s after returning; interpreter
        # shutdown alone took 1.2 s on a loaded machine.
        assert exited - returned <= 2.5, exited - returned

    @pytest.mark.parametrize("free_places", [1, 0], ids=["place free", "all taken"])
    async def test_a_call_cancelled_before_it_starts_sends_nothing(
        self, stub: CatalogStub, monkeypatch: pytest.MonkeyPatch, free_places: int
    ):
        # With every place taken the call would otherwise answer "unavailable"
        # at once; the caller must see its cancellation either way.
        places = threading.BoundedSemaphore(1)
        if not free_places:
            places.acquire()
        monkeypatch.setattr(remote_tools, "_OUTSTANDING", places)
        stub.call_answer = stub.json(200, _OK)
        tool = remote_tools.RemoteTool(
            name=_NAME,
            description="",
            parameters=_SCHEMA,
            call_url=remote_catalog.call_url({remote_catalog.URL_ENV: stub.url}),
        )
        returned = False

        with anyio.CancelScope() as scope:
            scope.cancel()
            await tool.run({"network": "one"})
            returned = True
        # Time for a request sent anyway to reach the stub.
        await asyncio.sleep(0.3)

        assert scope.cancelled_caught
        assert not returned
        assert stub.calls() == []

    async def test_a_cancelled_call_ends_and_the_server_keeps_answering(
        self, stub: CatalogStub, lifespan: None
    ):
        stub.call_answer = stub.drip(every=0.05)
        mcp = await _started()

        async with Client(mcp) as client:
            pending = asyncio.create_task(client.call_tool(_NAME, {"network": "one"}))
            while not stub.calls():
                await asyncio.sleep(0.02)
            # A browserless built-in answers while the remote call is pending.
            status = await asyncio.wait_for(
                client.call_tool("get_session_status", {}), 5
            )
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(pending, 1)
            again = await asyncio.wait_for(
                client.call_tool("get_session_status", {}), 5
            )

        assert status.structured_content is not None
        assert again.structured_content is not None


# --------------------------------------------------------------------------
# Middlewares
# --------------------------------------------------------------------------


class TestMiddlewares:
    async def test_a_remote_call_does_not_wait_for_a_held_browser(
        self, stub: CatalogStub, lifespan: None, tmp_path: Path
    ):
        os.environ["USER_DATA_DIR"] = str(tmp_path / "profile")
        os.environ["BROWSER_WAIT"] = "3"
        stub.call_answer = stub.json(200, _OK)
        mcp = await _started()

        holder = _hold_profile(tmp_path, 30)
        try:
            async with Client(mcp) as client:
                started = time.monotonic()
                result = await client.call_tool(_NAME, {"network": "one"})
                elapsed = time.monotonic() - started
        finally:
            holder.kill()
            holder.wait(timeout=10)

        assert _texts(result) == _OK["content"]
        assert elapsed < 1.0

    async def test_the_update_notice_waits_for_a_linkedin_tool(
        self,
        stub: CatalogStub,
        lifespan: None,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ):
        import linkedin_mcp_server.update_check as update_check

        os.environ["USER_DATA_DIR"] = str(tmp_path / "profile")
        monkeypatch.setattr(update_check, "__version__", "4.16.1")
        monkeypatch.setattr(update_check, "_latest_known", "4.18.0")
        prime = MagicMock()
        monkeypatch.setattr(update_check, "prime_from_cache", prime)
        monkeypatch.setattr(update_check, "refresh_latest_version", AsyncMock())
        notice = update_check.pending_update_notice()
        assert notice is not None
        stub.call_answer = stub.json(200, _OK)
        mcp = await _started()

        async with Client(mcp) as client:
            remote = await client.call_tool(_NAME, {"network": "one"})
            prime.assert_not_called()
            linkedin = await client.call_tool("close_session", {})

        assert notice not in [part.get("text") for part in _texts(remote)]
        assert notice in [part.get("text") for part in _texts(linkedin)]
