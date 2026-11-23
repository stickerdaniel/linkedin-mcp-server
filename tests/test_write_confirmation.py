import re
from typing import Any

import httpx2
import mcp.types as mt
import pytest
from fastmcp import Client, FastMCP
from fastmcp.client.elicitation import ElicitResult
from mcp.shared.exceptions import MCPError

from linkedin_mcp_server.error_handler import ErrorCodeMiddleware
from linkedin_mcp_server.write_confirmation import WriteConfirmationMiddleware

_BOTH_ERAS = pytest.mark.parametrize(
    ("mode", "protocol"),
    [("legacy", "2025-11-25"), ("auto", "2026-07-28")],
    ids=["handshake era", "2026-07-28 era"],
)

_WRITES = [
    ("connect_with_person", {"linkedin_username": "someone", "note": "Hi"}),
    (
        "send_message",
        {"linkedin_username": "someone", "message": "Hello", "confirm_send": True},
    ),
    (
        "reply_to_conversation",
        {"thread_id": "2-abc", "message": "Hello", "confirm_send": True},
    ),
    ("archive_conversation", {"thread_id": "2-abc"}),
    ("dismiss_job", {"job_id": "4252026496"}),
]


def _server(ran: list[tuple[str, dict[str, Any]]]) -> FastMCP:
    """The five write tools as stand-ins that record each run, behind the gate."""
    mcp = FastMCP("writes")
    mcp.add_middleware(ErrorCodeMiddleware())
    mcp.add_middleware(WriteConfirmationMiddleware())

    @mcp.tool
    async def connect_with_person(
        linkedin_username: str, note: str | None = None
    ) -> dict[str, bool]:
        ran.append(
            (
                "connect_with_person",
                {"linkedin_username": linkedin_username, "note": note},
            )
        )
        return {"done": True}

    @mcp.tool
    async def send_message(
        linkedin_username: str, message: str, confirm_send: bool
    ) -> dict[str, bool]:
        ran.append(
            (
                "send_message",
                {
                    "linkedin_username": linkedin_username,
                    "message": message,
                    "confirm_send": confirm_send,
                },
            )
        )
        return {"done": True}

    @mcp.tool
    async def reply_to_conversation(
        thread_id: str, message: str, confirm_send: bool
    ) -> dict[str, bool]:
        ran.append(
            (
                "reply_to_conversation",
                {
                    "thread_id": thread_id,
                    "message": message,
                    "confirm_send": confirm_send,
                },
            )
        )
        return {"done": True}

    @mcp.tool
    async def archive_conversation(thread_id: str) -> dict[str, bool]:
        ran.append(("archive_conversation", {"thread_id": thread_id}))
        return {"done": True}

    @mcp.tool
    async def dismiss_job(job_id: str) -> dict[str, bool]:
        ran.append(("dismiss_job", {"job_id": job_id}))
        return {"done": True}

    @mcp.tool
    async def get_inbox() -> dict[str, bool]:
        ran.append(("get_inbox", {}))
        return {"done": True}

    return mcp


class _User:
    """A client user who answers every prompt the same way and remembers each one."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    async def __call__(self, message: str, *_: Any) -> Any:
        self.prompts.append(message)
        return self.answer


async def _call(
    mode: str,
    protocol: str,
    name: str,
    arguments: dict[str, Any],
    user: _User | None,
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    ran: list[tuple[str, dict[str, Any]]] = []
    client = Client(_server(ran), mode=mode, elicitation_handler=user)
    async with client:
        assert client.protocol_version == protocol
        result = await client.call_tool(name, arguments, raise_on_error=False)
    return result, ran


def _code(result: Any) -> str | None:
    marker = (result.meta or {}).get("linkedin.dev/error")
    return marker["code"] if marker else None


@_BOTH_ERAS
@pytest.mark.parametrize(("name", "arguments"), _WRITES, ids=[w[0] for w in _WRITES])
async def test_an_accepted_write_runs_once_with_its_arguments(
    mode: str, protocol: str, name: str, arguments: dict[str, Any]
):
    user = _User({"confirm": True})

    result, ran = await _call(mode, protocol, name, arguments, user)

    assert not result.is_error
    assert len(user.prompts) == 1
    assert ran == [(name, arguments)]


@_BOTH_ERAS
@pytest.mark.parametrize(("name", "arguments"), _WRITES, ids=[w[0] for w in _WRITES])
@pytest.mark.parametrize(
    "answer",
    [
        ElicitResult(action="decline"),
        ElicitResult(action="cancel"),
        {"confirm": False},
    ],
    ids=["declined", "cancelled", "unticked"],
)
async def test_a_refused_write_never_runs(
    mode: str, protocol: str, name: str, arguments: dict[str, Any], answer: Any
):
    result, ran = await _call(mode, protocol, name, arguments, _User(answer))

    assert ran == []
    assert result.is_error
    assert _code(result) == "user_declined"


@_BOTH_ERAS
@pytest.mark.parametrize(("name", "arguments"), _WRITES, ids=[w[0] for w in _WRITES])
async def test_a_client_without_elicitation_is_served_as_before(
    mode: str, protocol: str, name: str, arguments: dict[str, Any]
):
    result, ran = await _call(mode, protocol, name, arguments, None)

    assert not result.is_error
    assert [tool for tool, _ in ran] == [name]


@_BOTH_ERAS
async def test_the_prompt_names_the_recipient_and_the_whole_message(
    mode: str, protocol: str
):
    user = _User({"confirm": True})
    message = "First line\n\nSecond paragraph"

    await _call(
        mode,
        protocol,
        "send_message",
        {"linkedin_username": "someone", "message": message, "confirm_send": True},
        user,
    )

    assert user.prompts == [
        f'Send this message to LinkedIn profile "someone"?\n\n{message}'
    ]


@_BOTH_ERAS
@pytest.mark.parametrize("name", ["send_message", "reply_to_conversation"])
async def test_a_dry_run_asks_nothing(mode: str, protocol: str, name: str):
    user = _User(ElicitResult(action="decline"))
    arguments = dict(dict(_WRITES)[name], confirm_send=False)

    result, ran = await _call(mode, protocol, name, arguments, user)

    assert not result.is_error
    assert user.prompts == []
    assert [tool for tool, _ in ran] == [name]


@_BOTH_ERAS
async def test_a_read_asks_nothing(mode: str, protocol: str):
    user = _User(ElicitResult(action="decline"))

    result, ran = await _call(mode, protocol, "get_inbox", {}, user)

    assert not result.is_error
    assert user.prompts == []
    assert ran == [("get_inbox", {})]


async def test_an_answer_counts_only_for_the_call_it_was_asked_for():
    ran: list[tuple[str, dict[str, Any]]] = []
    accept = {
        "confirm_write": mt.ElicitResult(action="accept", content={"confirm": True})
    }
    asked = {"linkedin_username": "someone", "message": "Hello", "confirm_send": True}
    changed = dict(asked, message="Something else")

    async with Client(
        _server(ran), mode="auto", elicitation_handler=_User({})
    ) as client:
        session = client.session
        unasked = await session.call_tool(
            "send_message", changed, input_responses=accept, allow_input_required=True
        )
        first = await session.call_tool(
            "send_message", asked, allow_input_required=True
        )
        assert isinstance(first, mt.InputRequiredResult)
        with pytest.raises(MCPError, match="requestState"):
            await session.call_tool(
                "send_message",
                changed,
                input_responses=accept,
                request_state=first.request_state,
                allow_input_required=True,
            )

    assert isinstance(unasked, mt.InputRequiredResult)
    assert ran == []


async def test_a_client_that_cannot_be_asked_back_gets_no_write():
    ran: list[tuple[str, dict[str, Any]]] = []
    app = _server(ran).http_app(json_response=True)
    headers = {
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    initialize = {
        "protocolVersion": "2025-11-25",
        "capabilities": {"elicitation": {"form": {}}},
        "clientInfo": {"name": "test", "version": "1"},
    }
    call = {"name": "send_message", "arguments": dict(dict(_WRITES)["send_message"])}

    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as http:
            opened = await http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": initialize,
                },
            )
            headers["Mcp-Session-Id"] = opened.headers["mcp-session-id"]
            await http.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            answered = await http.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": call,
                },
            )

    assert answered.json()["result"]["isError"] is True
    assert ran == []


@_BOTH_ERAS
@pytest.mark.parametrize(
    ("name", "target"),
    [
        ("connect_with_person", 'LinkedIn profile "someone"'),
        ("send_message", 'LinkedIn profile "someone"'),
        ("reply_to_conversation", 'LinkedIn conversation "2-abc"'),
        ("archive_conversation", 'LinkedIn conversation "2-abc"'),
        ("dismiss_job", 'LinkedIn job "4252026496"'),
    ],
)
async def test_the_prompt_names_what_the_write_acts_on_without_a_url(
    mode: str, protocol: str, name: str, target: str
):
    user = _User({"confirm": True})
    arguments = dict(dict(_WRITES)[name])
    reference = {
        "linkedin_username": "https://www.linkedin.com/in/someone/",
        "thread_id": "/messaging/thread/2-abc/",
        "job_id": "https://www.linkedin.com/jobs/view/4252026496/",
    }
    arguments.update({key: reference[key] for key in arguments if key in reference})

    await _call(mode, protocol, name, arguments, user)

    assert target in user.prompts[0]
    assert not re.search(r"https?://|www\.|linkedin\.com", user.prompts[0])


@_BOTH_ERAS
async def test_a_reference_that_names_no_profile_is_still_asked_about(
    mode: str, protocol: str
):
    user = _User(ElicitResult(action="decline"))
    arguments = {"linkedin_username": "/feed/", "message": "Hi", "confirm_send": True}

    result, ran = await _call(mode, protocol, "send_message", arguments, user)

    assert user.prompts == ['Send this message to LinkedIn profile "/feed/"?\n\nHi']
    assert ran == []
    assert _code(result) == "user_declined"
