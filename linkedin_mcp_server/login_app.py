"""Sign in to LinkedIn inside the chat, as an MCP App.

``open_login`` starts or joins the shared login and returns a view of it: the
host renders ``ui://linkedin-mcp/login.html`` (``login_app/viewer.html``). That
page calls ``login_view_attach`` for its grant, then polls ``login_view_frame``
for images of the login browser's page and sends the user's clicks, keys and
pastes through ``login_view_input``. The login itself is the ordinary one from
``bootstrap``; see ``login_view`` for how the page borrows its browser.

Trust model. ``open_login`` returns no grant. Its ``structuredContent`` names
the attempt and a non-secret ``open_id``; the page hands both to the app-only
``login_view_attach``, which returns the grant in its own result's ``_meta``.
The grant travels that way because Claude Desktop drops ``_meta`` from the
model-called result it hands to the app, while results of calls the app makes
keep it, and the model sees neither. The frame and input tools refuse a call
without the grant. MCP App visibility is not access control: any client on the
connection can list and call app-only tools. So the view is offered only where
one trusted peer holds the connection: the Direct role over stdio, to a client
that declared the MCP Apps extension. That client, Claude Desktop for one, is
trusted to keep app-only tools out of the model's tool list and ``_meta`` and
the app's traffic out of the model's context. An HTTP transport is never
eligible, since any local process could connect and ask for a grant.
"""

from __future__ import annotations

import base64
import functools
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Annotated, Literal

import mcp.types as mt
from fastmcp import Context, FastMCP
from fastmcp.apps.config import UI_EXTENSION_ID, AppConfig
from fastmcp.tools import ToolResult
from pydantic import Field

from linkedin_mcp_server.bootstrap import (
    NOT_OBSERVED,
    RuntimePolicy,
    current_login_attempt,
    current_login_generation,
    docker_auth_remedy,
    docker_dead_generation,
    get_runtime_policy,
    running_login_attempt,
    session_files_ready,
    start_login_if_needed,
)
from linkedin_mcp_server.config import get_config
from linkedin_mcp_server.error_handler import ErrorCode, error_result, raise_tool_error
from linkedin_mcp_server.exceptions import (
    AuthenticationInProgressError,
    AuthenticationStartedError,
)
from linkedin_mcp_server.login_view import (
    DEFAULT_BUDGET_S,
    LOGIN_VIEW_TOOLS,
    MAX_BUDGET_S,
    MAX_EVENTS,
    InputEvent,
    LoginAttempt,
)
from linkedin_mcp_server.login_viewer import (
    LoginViewerError,
    require_persistent_profile_mount,
)
from linkedin_mcp_server.server_role import ServerRole, process_role
from linkedin_mcp_server.session_state import PeerSessionInPlaceError

logger = logging.getLogger(__name__)

LOGIN_VIEW_URI = "ui://linkedin-mcp/login.html"
#: Where ``login_view_attach`` puts the grant: result ``_meta``, never content.
VIEW_META_KEY = "linkedin.dev/login-view"
GRANT_LIFETIME_S = 30 * 60.0
#: How long a grant still reads the outcome after its attempt ended.
STATUS_TAIL_S = 120.0

#: The vendored ``@modelcontextprotocol/ext-apps`` browser bundle, kept byte for
#: byte as published in 2.0.4 (``dist/src/app-with-deps.js``). Its license is
#: ``login_app/EXT_APPS_LICENSE``, and the zod 4.6.6 it bundles is under
#: ``login_app/ZOD_LICENSE``; ``tests/test_login_app_package.py`` pins
#: the digest.
SDK_FILE = "ext-apps-app-with-deps-2.0.4.js"
SDK_SHA256 = "d24b6ae50dbc989539db8287e217f41f0833f2b0a81ac4c6dbde444715ef5cd0"
_SDK_SLOT = "<!--EXT_APPS_SDK-->"
#: The bundle ends in one ``export{local as Name, ...}`` statement.
_SDK_EXPORTS = re.compile(r"export\{([^{}]*)\};?\s*\Z")

_SHOWN = (
    "The LinkedIn sign-in is shown in the chat. The user signs in there. Call "
    "the original tool again after the view says Signed in."
)
_SIGNED_IN = "A LinkedIn session is in place now. Retry the tool."
_FILES_EXIST = (
    "LinkedIn session files exist; retry the tool. If it fails again, call open_login."
)
_NOT_HERE = (
    "This client cannot show the LinkedIn sign-in. Run "
    "uvx mcp-server-linkedin@latest --login on this computer, then retry the "
    "tool."
)

Access = Literal["full", "status", "replaced", "expired", "denied"]


def view_available(ctx: Context | None) -> bool:
    """Whether this call's client may be given the sign-in view."""
    return (
        ctx is not None
        and process_role() is ServerRole.DIRECT
        and get_config().server.transport == "stdio"
        and ctx.client_supports_extension(UI_EXTENSION_ID)
    )


Attach = Literal["granted", "replaced", "none"]


@dataclass(slots=True)
class _Open:
    """One shown sign-in: what one ``open_login`` result hands to its page.

    *open_id* is not a secret; it names this open so that sibling views of
    one result share it and a newer ``open_login`` can supersede it. The grant
    is minted on the first attach, but its lifetime runs from the open.
    """

    open_id: str
    attempt: LoginAttempt
    issued_at: float
    secret: str | None = None


def _same(offered: str, expected: str | None) -> bool:
    return expected is not None and secrets.compare_digest(
        offered.encode(), expected.encode()
    )


class _Opens:
    """The current open of each attempt, and the opens a newer one superseded.

    A grant is 32 random bytes bound to one open of one attempt. It reads and
    drives the login browser for :data:`GRANT_LIFETIME_S` after the open, while
    the attempt runs, and reads only the outcome for :data:`STATUS_TAIL_S`
    after the attempt ended. Every attach of the current open returns the same
    grant and renews nothing, so a second view of the same result, or a retry,
    never takes access from the first. A newer open of the same attempt
    supersedes the old one: its grant and its ``open_id`` then read
    ``replaced``. Neither extends the login.
    """

    def __init__(self) -> None:
        self._current: dict[str, _Open] = {}
        self._superseded: dict[str, list[_Open]] = {}

    def open(self, attempt: LoginAttempt) -> _Open:
        now = time.monotonic()
        for attempt_id, current in list(self._current.items()):
            ended = current.attempt.ended_at
            if ended is not None and now - ended > STATUS_TAIL_S:
                del self._current[attempt_id]
                self._superseded.pop(attempt_id, None)
        previous = self._current.get(attempt.id)
        if previous is not None:
            self._superseded.setdefault(attempt.id, []).append(previous)
        opened = _Open(secrets.token_urlsafe(12), attempt, now)
        self._current[attempt.id] = opened
        return opened

    def attach(self, attempt_id: str, open_id: str) -> tuple[Attach, _Open | None]:
        """The grant of open *open_id* of attempt *attempt_id*, minted once.

        Answers only for that exact attempt and open; another attempt is never
        substituted. ``none`` for an unknown open, one whose grant expired, or
        an attempt that ended longer than :data:`STATUS_TAIL_S` ago.
        """
        current = self._current.get(attempt_id)
        if current is not None and _same(open_id, current.open_id):
            now = time.monotonic()
            ended = current.attempt.ended_at
            if now - current.issued_at > GRANT_LIFETIME_S or (
                ended is not None and now - ended > STATUS_TAIL_S
            ):
                return "none", None
            if current.secret is None:
                current.secret = secrets.token_urlsafe(32)
            return "granted", current
        for old in self._superseded.get(attempt_id, ()):
            if _same(open_id, old.open_id):
                return "replaced", None
        return "none", None

    def check(self, secret: str, attempt_id: str) -> tuple[Access, LoginAttempt | None]:
        """What *secret* may do with *attempt_id*, and that attempt if anything."""
        current = self._current.get(attempt_id)
        if current is None:
            return "denied", None
        if not _same(secret, current.secret):
            for old in self._superseded.get(attempt_id, ()):
                if _same(secret, old.secret):
                    return "replaced", None
            return "denied", None
        now = time.monotonic()
        if now - current.issued_at > GRANT_LIFETIME_S:
            return "expired", None
        ended = current.attempt.ended_at
        if ended is None:
            return "full", current.attempt
        if now - ended <= STATUS_TAIL_S:
            return "status", current.attempt
        return "expired", None


@functools.cache
def viewer_html() -> str:
    """The page the host renders, with the SDK inlined as a module script."""
    files = resources.files("linkedin_mcp_server") / "login_app"
    page = (files / "viewer.html").read_text(encoding="utf-8")
    sdk = (files / SDK_FILE).read_text(encoding="utf-8")
    return page.replace(_SDK_SLOT, _sdk_script(sdk), 1)


def _sdk_script(sdk: str) -> str:
    """Inline *sdk* unchanged and expose its ``App`` to the viewer's module.

    An inline module cannot import another inline one, and the bundle names
    ``App`` only in its closing ``export``. So the bundle stays as published,
    and one line appended after it publishes that binding on ``globalThis``
    under ``linkedinLoginSdk``, where the viewer reads it.
    """
    lowered = sdk.lower()
    if "</script" in lowered or "<!--" in sdk:
        raise ValueError("the SDK bundle cannot be inlined into a script element")
    exports = _SDK_EXPORTS.search(sdk)
    if exports is None:
        raise ValueError("the SDK bundle does not end in an export statement")
    names: dict[str, str] = {}
    for pair in exports.group(1).split(","):
        local, _, exported = pair.strip().partition(" as ")
        names[exported or local] = local
    app = names.get("App")
    if app is None or not app.replace("$", "_").isidentifier():
        raise ValueError("the SDK bundle does not export App")
    return (
        f'<script type="module">{sdk}\n'
        f"globalThis.linkedinLoginSdk = Object.freeze({{ App: {app} }});\n"
        "</script>"
    )


class _WithheldArguments(logging.Filter):
    """Keeps the view tools' arguments out of FastMCP's DEBUG call log.

    Those arguments carry the grant and whatever the user typed, a password
    included. FastMCP logs every call's arguments at DEBUG from
    ``fastmcp.server.mixins.mcp_operations``. The filter sits on that emitting
    logger, because a filter on a parent logger never sees a record a child
    logger propagates.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if (
            isinstance(args, tuple)
            and len(args) == 2
            and args[0] in LOGIN_VIEW_TOOLS
            and "call_tool" in str(record.msg)
        ):
            record.args = (args[0], "<withheld>")
        return True


_CALL_LOGGER = "fastmcp.server.mixins.mcp_operations"


def _withhold_view_arguments() -> None:
    target = logging.getLogger(_CALL_LOGGER)
    if not any(isinstance(f, _WithheldArguments) for f in target.filters):
        target.addFilter(_WithheldArguments())


def _reply(payload: dict[str, object], image: bytes | None = None) -> ToolResult:
    content: list[mt.TextContent | mt.ImageContent] = [
        mt.TextContent(type="text", text=json.dumps(payload))
    ]
    if image is not None:
        content.append(
            mt.ImageContent(
                type="image",
                data=base64.b64encode(image).decode("ascii"),
                mime_type="image/jpeg",
            )
        )
    return ToolResult(content=content)


def _status(access: Access, attempt: LoginAttempt, frame: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "access": access,
        "state": attempt.state,
        "frame": frame,
    }
    if attempt.reason is not None:
        payload["reason"] = attempt.reason
    return payload


def _not_offered(ctx: Context | None) -> str:
    if get_runtime_policy() == RuntimePolicy.DOCKER:
        return docker_auth_remedy(ctx)
    return _NOT_HERE


async def _start_or_join(ctx: Context) -> LoginAttempt | str:
    """The attempt to show, or the words that replace the view.

    In this order: a running login wins and is joined. A session a Docker tool
    found dead is retired and a login started for it. Session files that no tool
    found dead are left alone, since nothing says they fail. Otherwise the
    shared login starts, after an auto-import where one is allowed.
    """
    joined = running_login_attempt()
    if joined is not None:
        return joined
    try:
        dead = docker_dead_generation()
        if dead is not NOT_OBSERVED:
            from linkedin_mcp_server.dependencies import restart_dead_docker_login

            await restart_dead_docker_login(ctx, generation=_generation(dead))  # raises
        if session_files_ready():
            return _FILES_EXIST
        await start_login_if_needed(
            ctx, superseded_by=current_login_generation(), inline_wait=False
        )
        # Returned without starting anything: an import or a peer put a session
        # in place.
        return _SIGNED_IN
    except (AuthenticationInProgressError, AuthenticationStartedError):
        attempt = current_login_attempt()
        if attempt is None:
            raise
        return attempt
    except PeerSessionInPlaceError:
        return _SIGNED_IN


def _generation(observed: str | None | object) -> str | None:
    return observed if isinstance(observed, str) else None


def register_login_app(mcp: FastMCP, *, tool_timeout: float) -> None:
    """Register ``open_login``, the three view tools and the view's page on *mcp*.

    Only for the Direct role: an owner has no client to show a view to, and a
    proxy forwards tools without the page they need.
    """
    _withhold_view_arguments()
    grants = _Opens()

    @mcp.resource(
        LOGIN_VIEW_URI,
        name="login_view",
        title="LinkedIn sign-in",
        description="The page open_login shows to sign in to LinkedIn.",
        app=AppConfig(prefers_border=True),
    )
    def login_view_page() -> str:
        return viewer_html()

    @mcp.tool(
        timeout=tool_timeout,
        title="Sign in to LinkedIn",
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "openWorldHint": True,
        },
        tags={"session"},
        app=AppConfig(resource_uri=LOGIN_VIEW_URI),
    )
    async def open_login(ctx: Context) -> ToolResult:
        """Show the LinkedIn sign-in inside this chat, so the user can sign in there.

        Call it when a tool says to call open_login. The user signs in in the
        view; call the original tool again once the view says Signed in.
        """
        if not view_available(ctx):
            return error_result(_not_offered(ctx), ErrorCode.AUTH_REQUIRED)
        if get_runtime_policy() == RuntimePolicy.DOCKER:
            # Before anything moves: a session signed in on the container's own
            # filesystem dies with it.
            try:
                require_persistent_profile_mount(
                    Path(get_config().browser.user_data_dir)
                )
            except LoginViewerError as exc:
                return error_result(str(exc), ErrorCode.AUTH_REQUIRED)
        try:
            shown = await _start_or_join(ctx)
        except Exception as exc:
            raise_tool_error(exc, "open_login")
        if isinstance(shown, str):
            return ToolResult(content=[mt.TextContent(type="text", text=shown)])
        opened = grants.open(shown)
        # No grant here: the page asks login_view_attach for it with these
        # two non-secret names.
        return ToolResult(
            content=[mt.TextContent(type="text", text=_SHOWN)],
            structured_content={
                "view": "shown",
                "attempt_id": shown.id,
                "open_id": opened.open_id,
            },
        )

    @mcp.tool(
        name="login_view_attach",
        timeout=10,
        annotations={"readOnlyHint": True, "openWorldHint": False},
        app=AppConfig(visibility=["app"]),
    )
    async def login_view_attach(
        ctx: Context,
        attempt_id: Annotated[str, Field(max_length=64)],
        open_id: Annotated[str, Field(max_length=64)],
    ) -> ToolResult:
        """Called by the sign-in view: the grant for the open its result named."""
        if not view_available(ctx):
            return _reply({"access": "none"})
        outcome, opened = grants.attach(attempt_id, open_id)
        if opened is None or opened.secret is None:
            return _reply({"access": outcome})
        return ToolResult(
            content=[mt.TextContent(type="text", text=json.dumps({"access": outcome}))],
            meta={
                VIEW_META_KEY: {
                    "grant": opened.secret,
                    "attempt_id": opened.attempt.id,
                    "open_id": opened.open_id,
                }
            },
        )

    @mcp.tool(
        name="login_view_frame",
        timeout=MAX_BUDGET_S + 10,
        annotations={"readOnlyHint": True, "openWorldHint": False},
        app=AppConfig(visibility=["app"]),
    )
    async def login_view_frame(
        grant: Annotated[str, Field(max_length=128)],
        attempt_id: Annotated[str, Field(max_length=64)],
        after: Annotated[str, Field(max_length=64)] = "",
        budget_s: Annotated[float, Field(ge=0, le=MAX_BUDGET_S)] = DEFAULT_BUDGET_S,
    ) -> ToolResult:
        """Called by the sign-in view: the page once it differs from frame *after*."""
        access, attempt = grants.check(grant, attempt_id)
        if attempt is None:
            return _reply({"access": access})
        view = attempt.view if access == "full" else None
        if access == "full" and view is None and attempt.outcome is None:
            # Not published yet (installing, waiting for the profile, opening
            # the browser) or already hidden on the way out.
            await attempt.wait_for_change(budget_s)
            return _reply(_status(access, attempt, "none"))
        if view is None:
            return _reply(_status(access, attempt, "none"))
        result = await view.frame(after or None, budget_s)
        # Again, right before an image leaves: the grant may have been replaced
        # or the login may have ended while the capture ran.
        access, attempt = grants.check(grant, attempt_id)
        if attempt is None:
            return _reply({"access": access})
        if access != "full" or result.frame is None:
            status = "none" if result.status == "closed" else result.status
            return _reply(_status(access, attempt, status))
        payload = _status(access, attempt, "image")
        payload.update(
            hash=result.frame.digest,
            page_id=result.frame.page_id,
            geometry=result.frame.geometry,
        )
        return _reply(payload, result.frame.jpeg)

    @mcp.tool(
        name="login_view_input",
        timeout=30,
        annotations={"readOnlyHint": False, "openWorldHint": True},
        app=AppConfig(visibility=["app"]),
    )
    async def login_view_input(
        grant: Annotated[str, Field(max_length=128)],
        attempt_id: Annotated[str, Field(max_length=64)],
        page_id: Annotated[str, Field(max_length=16)],
        geometry: Annotated[str, Field(pattern=r"^[0-9]{1,5}x[0-9]{1,5}$")],
        events: Annotated[list[InputEvent], Field(min_length=1, max_length=MAX_EVENTS)],
    ) -> ToolResult:
        """Called by the sign-in view: replay the user's input into the page."""
        access, attempt = grants.check(grant, attempt_id)
        view = attempt.view if attempt is not None and access == "full" else None
        if view is None:
            return _reply({"access": access, "input": "refused"})
        result = await view.apply(
            page_id,
            geometry,
            events,
            still_allowed=lambda: grants.check(grant, attempt_id)[0] == "full",
        )
        access, _ = grants.check(grant, attempt_id)
        return _reply(
            {"access": access, "input": result.status, "applied": result.applied}
        )
