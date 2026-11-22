"""The user confirms each LinkedIn write in the client before it runs.

The caller of a tool is a model, and the model reads LinkedIn text that other
people wrote, so an argument such as ``confirm_send`` is the model's answer,
not the user's. A form elicitation is answered by the user, and the model sees
only the outcome. A client that declares no form elicitation is served as
before, with its own tool approval as the gate.

Installed only on a server that faces a client, outside the proxy's forwarding
and the browser lock, so the wait for an answer holds neither and counts
against no tool timeout.

On 2026-07-28 the question is asked through an ``InputRequiredResult``. The SDK
seals its ``request_state`` and binds it to the tool name, the arguments and
the caller, so an answer counts only for the call it was asked for.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any

import mcp.types as mt
from fastmcp.server.context import Context
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from fastmcp.tools.base import InputRequiredToolResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from pydantic import TypeAdapter, ValidationError

from linkedin_mcp_server.error_handler import CodedToolError, ErrorCode

_INPUT_KEY = "confirm_write"

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "confirm": {
            "type": "boolean",
            "title": "Yes, do this on LinkedIn",
            "default": True,
        }
    },
}

_BOOL = TypeAdapter(bool)


def _quoted(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _is_dry_run(arguments: Mapping[str, Any]) -> bool:
    try:
        return _BOOL.validate_python(arguments.get("confirm_send")) is False
    except ValidationError:
        return False


def _connect(arguments: Mapping[str, Any]) -> str:
    prompt = (
        f"Send a LinkedIn connection invitation to "
        f"{_quoted(arguments.get('linkedin_username'))}, or accept theirs?"
    )
    note = arguments.get("note")
    return prompt if note is None else f"{prompt}\n\nNote:\n{note}"


def _send(arguments: Mapping[str, Any]) -> str | None:
    if _is_dry_run(arguments):
        return None
    return (
        f"Send this LinkedIn message to "
        f"{_quoted(arguments.get('linkedin_username'))}?\n\n{arguments.get('message')}"
    )


def _reply(arguments: Mapping[str, Any]) -> str | None:
    if _is_dry_run(arguments):
        return None
    return (
        f"Send this reply in LinkedIn conversation "
        f"{_quoted(arguments.get('thread_id'))}?\n\n{arguments.get('message')}"
    )


def _archive(arguments: Mapping[str, Any]) -> str:
    return f"Archive LinkedIn conversation {_quoted(arguments.get('thread_id'))}?"


def _dismiss(arguments: Mapping[str, Any]) -> str:
    return (
        f"Dismiss LinkedIn job {_quoted(arguments.get('job_id'))} from job search? "
        "LinkedIn offers no undo."
    )


PROMPTS: dict[str, Callable[[Mapping[str, Any]], str | None]] = {
    "connect_with_person": _connect,
    "send_message": _send,
    "reply_to_conversation": _reply,
    "archive_conversation": _archive,
    "dismiss_job": _dismiss,
}


def _client_answers_forms(ctx: Context) -> bool:
    capabilities = ctx.session.client_capabilities
    elicitation = capabilities.elicitation if capabilities is not None else None
    if elicitation is None:
        return False
    return elicitation.form is not None or elicitation.url is None


def _is_modern(ctx: Context) -> bool:
    request = ctx.request_context
    return request is not None and request.protocol_version in MODERN_PROTOCOL_VERSIONS


def _confirmed(answer: mt.ElicitResult) -> bool:
    content = answer.content or {}
    return answer.action == "accept" and content.get("confirm", True) is True


def _declined(name: str) -> CodedToolError:
    return CodedToolError(
        f"The user declined {name} in the client, so nothing was done on "
        "LinkedIn. Call it again only if the user asks for it again.",
        ErrorCode.USER_DECLINED,
    )


class WriteConfirmationMiddleware(Middleware):
    """Ask the user before a write tool runs, where the client can ask."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        name = context.message.name
        arguments = context.message.arguments or {}
        prompt_for = PROMPTS.get(name)
        prompt = prompt_for(arguments) if prompt_for is not None else None
        ctx = context.fastmcp_context
        if prompt is None or ctx is None or not _client_answers_forms(ctx):
            return await call_next(context)

        if _is_modern(ctx):
            answer = (ctx.input_responses or {}).get(_INPUT_KEY)
            if ctx.request_state != _INPUT_KEY or answer is None:
                return InputRequiredToolResult(
                    mt.InputRequiredResult(
                        input_requests={
                            _INPUT_KEY: mt.ElicitRequest(
                                params=mt.ElicitRequestFormParams(
                                    message=prompt, requested_schema=_SCHEMA
                                )
                            )
                        },
                        request_state=_INPUT_KEY,
                    )
                )
            if not isinstance(answer, mt.ElicitResult) or not _confirmed(answer):
                raise _declined(name)
            return await call_next(context)

        try:
            answer = await ctx.session.elicit_form(
                prompt, _SCHEMA, related_request_id=ctx.request_id
            )
        except Exception as exc:
            raise CodedToolError(
                f"The client could not ask the user to confirm {name}, so "
                "nothing was done on LinkedIn.",
                ErrorCode.INTERNAL,
            ) from exc
        if not _confirmed(answer):
            raise _declined(name)
        return await call_next(context)
