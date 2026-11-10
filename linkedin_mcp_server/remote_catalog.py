"""Optional tools from a remote catalog: the request, its gates and its contract.

At start the server may carry tools it does not ship. One GET asks the
catalog URL for their definitions and one line for the server
instructions, and each call to such a tool is a POST to the same URL with
``/call`` appended. ``remote_tools`` turns the answer into FastMCP tools.

Everything at module level here is stdlib, because :func:`prefetch` runs
before ``cli_main`` is imported: that import is most of the start time, and
the GET runs in parallel with it. Usage:

* ``__main__.main`` calls :func:`prefetch` with ``sys.argv[1:]`` first.
* ``remote_tools.install`` calls :func:`awaited_body` once the server exists,
  which waits at most until the start deadline, and then :func:`admit`.
* ``remote_tools.RemoteTool.run`` calls :func:`call` in a daemon thread.

Set ``LINKEDIN_MCP_REMOTE_TOOLS=off`` to turn all of it off.
"""

from __future__ import annotations

import http.client
import io
import json
import locale
import math
import os
import platform
import re
import socket
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, TypeGuard
from urllib.parse import urlsplit

from linkedin_mcp_server import __version__

if TYPE_CHECKING:
    import urllib.request

    from _typeshed import WriteableBuffer

CATALOG_URL = "https://api.cadenza.page/oss/tools"
URL_ENV = "LINKEDIN_MCP_REMOTE_TOOLS_URL"

#: How long a start waits for the catalog, from the first line of ``main()``.
BUDGET_SECONDS = 2.0

UNAVAILABLE_TEXT = "This tool is unavailable right now."

_OFF_VALUES = frozenset({"0", "false", "off", "no"})
# The same spellings `config/loaders.py` reads as true for DAEMON_ENABLED.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

#: Bounds the whole catalog; the catalog itself decides how many tools it serves.
_CATALOG_MAX_BYTES = 64 * 1024
_CALL_MAX_BYTES = 256 * 1024
_CHUNK_BYTES = 16 * 1024

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_MAX_DESCRIPTION = 4096
_MAX_TITLE = 100
_MAX_INSTRUCTIONS = 1000
_MAX_PROPERTIES = 20
_MAX_PROPERTY_DESCRIPTION = 500
_MAX_ENUM = 50
_MAX_CLIENT_HEADER = 100
_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

#: Every long option of the parser in ``config/loaders.py``, ``--help``
#: included. argparse accepts any unambiguous prefix of one of these, so the
#: early gate below needs all of them to resolve a prefix the same way.
#: ``tests/test_remote_catalog.py`` compares this list with the real parser.
LONG_OPTIONS = (
    "--help",
    "--no-headless",
    "--log-level",
    "--transport",
    "--host",
    "--port",
    "--path",
    "--slow-mo",
    "--user-agent",
    "--viewport",
    "--timeout",
    "--tool-timeout",
    "--login-timeout",
    "--login-inline-wait",
    "--browser-wait",
    "--browser-min-hold",
    "--browser-idle-timeout",
    "--chrome-path",
    "--proxy-server",
    "--proxy-username",
    "--proxy-bypass",
    "--login",
    "--login-viewer",
    "--status",
    "--logout",
    "--user-data-dir",
    "--installer-temp-dir",
    "--claim-profile-root",
    "--import-from-browser",
    "--auto-import",
    "--no-auto-import",
    "--eager-full-chromium",
    "--no-eager-full-chromium",
    "--daemon",
    "--no-daemon",
)

#: Commands that end before a server runs.
_NEVER_SERVES = frozenset(
    {"--help", "--login", "--logout", "--status", "--import-from-browser"}
)
#: The environment form of ``--import-from-browser``. The loader reads any
#: non-empty raw value as the command, whitespace included (as ``auto``).
_IMPORT_ENV = "IMPORT_FROM_BROWSER"


# --------------------------------------------------------------------------
# Who is asking
# --------------------------------------------------------------------------


def install_kind(environ: Mapping[str, str] = os.environ) -> str:
    """``docker`` or ``mcpb`` as the launcher declares it, else ``managed``.

    The Dockerfile and the bundle manifest set ``LINKEDIN_MCP_RUNTIME``, so no
    container detection runs this early.
    """
    value = environ.get("LINKEDIN_MCP_RUNTIME", "").strip().lower()
    return value if value in ("docker", "mcpb") else "managed"


def _token(value: str) -> str:
    """*value* reduced to characters that cannot end a User-Agent segment."""
    return re.sub(r"[^A-Za-z0-9._-]", "", value)


def _os_and_release() -> tuple[str, str]:
    if sys.platform == "darwin":
        return "macos", platform.mac_ver()[0]
    if sys.platform == "win32":
        # Never platform.uname() or platform.system() here: both run WMI
        # queries on Windows, which can take the process down (#838).
        version = sys.getwindowsversion()
        return "windows", f"{version.major}.{version.build}"
    if sys.platform.startswith("linux"):
        try:
            release = platform.freedesktop_os_release()
        except OSError:
            return "linux", ""
        parts = (release.get("ID", ""), release.get("VERSION_ID", ""))
        return "linux", "-".join(part for part in parts if part)
    return sys.platform, ""


def _arch() -> str:
    if sys.platform == "win32":
        # platform.machine() is platform.uname() and so WMI on Windows; the
        # environment names the native architecture even under WOW64.
        machine = os.environ.get("PROCESSOR_ARCHITEW6432") or os.environ.get(
            "PROCESSOR_ARCHITECTURE", ""
        )
    else:
        machine = platform.machine()
    value = machine.lower()
    # The spellings `session_state._normalize_arch` uses for the runtime id.
    if value in {"x86_64", "amd64"}:
        return "amd64"
    if value in {"arm64", "aarch64"}:
        return "arm64"
    return value or "unknown"


def user_agent(
    transport: str | None = None, environ: Mapping[str, str] = os.environ
) -> str:
    """``mcp-server-linkedin/<version> (<install>; <platform>; py<x.y>[; <transport>])``.

    The catalog GET leaves before the transport is known, so only a call
    names it. ``<platform>`` is ``<os>-<release>-<arch>``, with the release
    left out when the system does not name one.
    """
    os_name, release = _os_and_release()
    platform_id = "-".join(
        _token(part) for part in (os_name, release, _arch()) if _token(part)
    )
    python = f"py{sys.version_info.major}.{sys.version_info.minor}"
    segments = [install_kind(environ), platform_id, python]
    if transport is not None:
        segments.append(_token(transport))
    return f"mcp-server-linkedin/{_token(__version__)} ({'; '.join(segments)})"


def accept_language(environ: Mapping[str, str] = os.environ) -> str | None:
    """The system locale as a BCP 47 tag, or ``None`` when there is none.

    POSIX order: the first of ``LC_ALL``, ``LC_MESSAGES`` and ``LANG`` that is
    set decides, so ``de_DE.UTF-8`` gives ``de-DE``. Without any of them the
    interpreter's locale is asked. ``C``, ``POSIX`` and anything that is not
    shaped like a language tag give ``None``.
    """
    value = next(
        (environ[key] for key in ("LC_ALL", "LC_MESSAGES", "LANG") if environ.get(key)),
        None,
    )
    if value is None:
        try:
            value = locale.getlocale()[0]
        except ValueError:
            value = None
    if not value:
        return None
    tag = value.split(".", 1)[0].split("@", 1)[0].replace("_", "-")
    if tag in ("C", "POSIX") or not re.fullmatch(
        r"[A-Za-z]{2,8}(-[A-Za-z0-9]{1,8})*", tag
    ):
        return None
    return tag


def client_header(name: str | None, version: str | None) -> str | None:
    """The ``X-MCP-Client`` value for a client's ``clientInfo``, or ``None``.

    Printable ASCII only, at most 100 characters; ``None`` when nothing is
    left, so the header is omitted rather than sent empty.
    """
    if not name:
        return None
    raw = f"{name}/{version}" if version else name
    value = "".join(ch for ch in raw if " " <= ch <= "~")[:_MAX_CLIENT_HEADER]
    return value.strip() or None


# --------------------------------------------------------------------------
# Whether to ask at all
# --------------------------------------------------------------------------


def opted_out(environ: Mapping[str, str] = os.environ) -> bool:
    """Whether the user turned this off. Always final, the URL override too.

    ``LINKEDIN_MCP_CHECK_FOR_UPDATES=off`` counts as well: a user who stopped
    the server from asking PyPI did not mean it to ask somebody else.
    """
    return any(
        environ.get(key, "").strip().lower() in _OFF_VALUES
        for key in ("LINKEDIN_MCP_REMOTE_TOOLS", "LINKEDIN_MCP_CHECK_FOR_UPDATES")
    )


def _resolve(token: str) -> str | None:
    """The long option argparse reads *token* as, or ``None``.

    The parser's own rules: an exact name, ``name=value``, or a prefix that
    matches exactly one long option. Any ``-h`` form is help, the only short
    option the parser has.
    """
    if token.startswith("--"):
        name = token.split("=", 1)[0]
        if name in LONG_OPTIONS:
            return name
        matches = [option for option in LONG_OPTIONS if option.startswith(name)]
        return matches[0] if len(matches) == 1 else None
    if token.startswith("-h"):
        return "--help"
    return None


def launch_serves(argv: Sequence[str], environ: Mapping[str, str] = os.environ) -> bool:
    """Whether this command line runs a server that may load remote tools.

    False for a command that ends before serving, from the command line or
    from ``IMPORT_FROM_BROWSER``, for an unknown or ambiguous long option,
    which the parser refuses, and for every launch the daemon is
    configured for. That is deliberately conservative: some of those end up
    serving directly, and they simply get no remote tools. ``--daemon`` or
    ``--no-daemon`` on the command line wins over ``DAEMON_ENABLED``, as in
    the real parser; that parser refuses the two together, so no valid launch
    carries both.
    """
    if environ.get(_IMPORT_ENV):
        return False
    daemon: bool | None = None
    for token in argv:
        option = _resolve(token)
        # argparse reads a token with a space as a value, never as an option.
        refused = (
            option is None
            and token.startswith("--")
            and token != "--"
            and " " not in token
        )
        if refused or option in _NEVER_SERVES:
            return False
        if option == "--daemon":
            daemon = True
        elif option == "--no-daemon":
            daemon = False
    if daemon is None:
        daemon = environ.get("DAEMON_ENABLED", "").strip().lower() in _TRUE_VALUES
    return not daemon


# --------------------------------------------------------------------------
# The catalog request
# --------------------------------------------------------------------------


@dataclass
class _Fetch:
    """One catalog GET. The thread fills it and then sets *done*."""

    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    body: bytes | None = None
    completed_at: float | None = None


_pending: _Fetch | None = None


def configured_url(environ: Mapping[str, str] = os.environ) -> str:
    return environ.get(URL_ENV, "").strip() or CATALOG_URL


def call_url(environ: Mapping[str, str] = os.environ) -> str:
    return configured_url(environ).rstrip("/") + "/call"


def prefetch(argv: Sequence[str]) -> None:
    """Start the catalog GET in a daemon thread, or decide not to. Returns at once.

    Gates, in order: the ``.env`` file is loaded first so an opt-out there
    counts; an opt-out ends it; so does a command that never serves or a
    daemon-configured launch. Every install asks alike: a source checkout
    and CI too. The thread never logs and never writes to stdout, which
    belongs to the protocol.

    Never raises: it runs before the server is even imported, so an
    exception here would stop the built-in tools too. Any failure leaves no
    request pending, and the server starts without remote tools.
    """
    global _pending
    try:
        _prefetch(argv)
    except Exception:  # noqa: BLE001 - optional; the server must start regardless
        _pending = None


def _prefetch(argv: Sequence[str]) -> None:
    global _pending
    origin = time.monotonic()
    _pending = None
    # The same call `config/loaders.py` makes at import; it never overrides a
    # variable that is already set, so loading it early changes nothing later.
    import dotenv

    dotenv.load_dotenv()
    environ = os.environ
    if opted_out(environ) or not launch_serves(argv, environ):
        return
    url = configured_url(environ)
    # An override such as ``http://[`` raises here; `prefetch` absorbs it.
    if urlsplit(url).scheme not in ("http", "https"):
        return
    headers = {"User-Agent": user_agent(environ=environ), "Accept": "application/json"}
    if language := accept_language(environ):
        headers["Accept-Language"] = language
    fetch = _Fetch(deadline=origin + BUDGET_SECONDS)
    _pending = fetch
    threading.Thread(
        target=_fetch_catalog,
        args=(url, headers, fetch),
        name="linkedin-remote-catalog",
        daemon=True,
    ).start()


def _fetch_catalog(url: str, headers: dict[str, str], fetch: _Fetch) -> None:
    try:
        status, body = _request(
            url, headers=headers, timeout=BUDGET_SECONDS, limit=_CATALOG_MAX_BYTES
        )
        if status == 200:
            fetch.completed_at = time.monotonic()
            fetch.body = body
    except BaseException:  # noqa: BLE001 - a daemon thread with nobody to tell
        pass
    finally:
        fetch.done.set()


def requested() -> bool:
    """Whether :func:`prefetch` sent the catalog GET in this process."""
    return _pending is not None


def awaited_body() -> bytes | None:
    """The catalog body, if it arrived complete before the start deadline.

    Waits until the deadline at most. ``None`` when no request was sent, it
    failed, or it finished too late.
    """
    fetch = _pending
    if fetch is None:
        return None
    fetch.done.wait(max(0.0, fetch.deadline - time.monotonic()))
    completed_at = fetch.completed_at
    if completed_at is None or completed_at > fetch.deadline:
        return None
    return fetch.body


# --------------------------------------------------------------------------
# HTTP with refused redirects and bounded reads
# --------------------------------------------------------------------------


class _DeadlineReader(io.RawIOBase):
    """A socket reader that gives every receive the time left until *deadline*.

    It wraps the response's own ``SocketIO`` rather than the socket, so the
    socket stays counted as in use until the response closes it: urllib
    closes its connection object as soon as the headers are in.
    """

    def __init__(self, raw: io.RawIOBase, sock: socket.socket, deadline: float):
        super().__init__()
        self._raw = raw
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: WriteableBuffer, /) -> int | None:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("remote tool deadline passed")
        self._sock.settimeout(remaining)
        return self._raw.readinto(buffer)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


def _arm(sock: socket.socket | None, deadline: float) -> None:
    """Give *sock* the time left until *deadline*, or refuse to use it.

    Runs once the connection is up (resolution, TCP and TLS included) and
    before a request byte is sent: resolution takes no timeout at all, so it
    can outlast the deadline, and a request sent after that would act on
    behalf of a caller who was already told the tool is unavailable.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("remote tool deadline passed before sending")
    if sock is not None:
        sock.settimeout(remaining)


class _DeadlineResponse(http.client.HTTPResponse):
    """An HTTP response whose header and body reads both honour :attr:`deadline`."""

    deadline: ClassVar[float]

    def __init__(
        self,
        sock: socket.socket,
        debuglevel: int = 0,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(sock, debuglevel, method, url)
        # Nothing is buffered yet, so detaching hands over the bare SocketIO.
        raw = self.fp.detach()
        self.fp = io.BufferedReader(_DeadlineReader(raw, sock, self.deadline))


def _opener(deadline: float | None) -> urllib.request.OpenerDirector:
    """An opener that never follows a redirect, with deadline-bound sockets.

    Every status comes back as a response: the stock error processor is what
    hands a 3xx to the redirect handler, and it is replaced by one that passes
    everything through. A redirect therefore stays a non-200 answer, and no
    request leaves the configured origin. Environment proxies still apply, as
    for any urllib request.
    """
    import urllib.request

    class KeepEveryStatus(urllib.request.HTTPErrorProcessor):
        def http_response(
            self, request: urllib.request.Request, response: http.client.HTTPResponse
        ) -> http.client.HTTPResponse:
            return response

        https_response = http_response

    handlers: list[urllib.request.BaseHandler] = [KeepEveryStatus()]
    if deadline is not None:

        class Response(_DeadlineResponse):
            pass

        Response.deadline = deadline

        class HTTPConnection(http.client.HTTPConnection):
            response_class = Response

            def connect(self) -> None:
                super().connect()
                _arm(self.sock, deadline)

        class HTTPSConnection(http.client.HTTPSConnection):
            response_class = Response

            def connect(self) -> None:
                super().connect()
                _arm(self.sock, deadline)

        class HTTPHandler(urllib.request.HTTPHandler):
            def http_open(
                self, req: urllib.request.Request
            ) -> http.client.HTTPResponse:
                return self.do_open(HTTPConnection, req)

        class HTTPSHandler(urllib.request.HTTPSHandler):
            def https_open(
                self, req: urllib.request.Request
            ) -> http.client.HTTPResponse:
                return self.do_open(HTTPSConnection, req)

        handlers += [HTTPHandler(), HTTPSHandler()]
    return urllib.request.build_opener(*handlers)


def _request(
    url: str,
    *,
    headers: Mapping[str, str],
    limit: int,
    timeout: float,
    data: bytes | None = None,
    deadline: float | None = None,
) -> tuple[int, bytes]:
    """Send one request and read at most *limit* bytes of the answer.

    *timeout* bounds the connect and every socket operation; with a
    *deadline*, nothing is sent once it has passed, each later operation gets
    the time left until it, and the read stops once it passes. A longer body
    is an error, not a truncation.
    """
    import urllib.request

    request = urllib.request.Request(url, data=data, headers=dict(headers))
    with _opener(deadline).open(request, timeout=timeout) as response:
        status: int = response.status
        chunks: list[bytes] = []
        size = 0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("remote tool deadline passed")
            chunk = response.read1(min(_CHUNK_BYTES, limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise ValueError("response is larger than allowed")
            chunks.append(chunk)
    return status, b"".join(chunks)


# --------------------------------------------------------------------------
# Admission
# --------------------------------------------------------------------------


class RejectedCatalog(ValueError):
    """The catalog breaks the admission contract; nothing of it is used."""


_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean"})
#: The keywords each property type may carry beyond ``type``,
#: ``description`` and ``default``.
_KEYWORDS: dict[str, frozenset[str]] = {
    "string": frozenset({"minLength", "maxLength", "enum"}),
    "integer": frozenset({"minimum", "maximum", "enum"}),
    "number": frozenset({"minimum", "maximum", "enum"}),
    "boolean": frozenset({"enum"}),
    "array": frozenset({"items", "maxItems"}),
}


@dataclass(frozen=True)
class ToolSpec:
    """One admitted tool, holding only the fields the contract allows."""

    name: str
    description: str
    input_schema: dict[str, object]
    title: str | None = None
    annotations: dict[str, bool | str] = field(default_factory=dict)


@dataclass(frozen=True)
class Catalog:
    instructions: str
    tools: tuple[ToolSpec, ...]


def _finite(text: str) -> float:
    """``parse_float`` for :func:`_loads`: ``1e999`` would otherwise be infinity."""
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"number out of range: {text}")
    return value


def _no_constant(name: str) -> object:
    """``parse_constant`` for :func:`_loads`: ``NaN`` and ``Infinity`` are not JSON."""
    raise ValueError(f"not a JSON number: {name}")


def _loads(body: bytes) -> object:
    """*body* as strict JSON: every number it holds is finite.

    The stock parser accepts ``NaN``, ``Infinity`` and overflowing exponents.
    None of them survives serialization as the number it was, so a schema
    bound such as ``minimum: NaN`` would reach the client as ``null``.
    """
    return json.loads(body, parse_constant=_no_constant, parse_float=_finite)


def _is_text(value: object) -> TypeGuard[str]:
    """Whether *value* is a string that encodes as UTF-8.

    A JSON ``\\ud800`` escape decodes to a lone surrogate, which is a Python
    string but no Unicode text: the transport cannot encode it, and the first
    message carrying it ends the session, built-in tools included.
    """
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _is_of_type(value: object, kind: str) -> bool:
    if kind == "string":
        return _is_text(value)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _short_text(value: object, limit: int) -> bool:
    return _is_text(value) and len(value) <= limit


def _check_property(name: str, schema: object) -> None:
    if _NAME.fullmatch(name) is None:
        raise RejectedCatalog(f"property name {name!r}")
    if not isinstance(schema, dict):
        raise RejectedCatalog(f"property {name!r} is not an object")
    kind = schema.get("type")
    if not isinstance(kind, str) or kind not in _KEYWORDS:
        raise RejectedCatalog(f"property {name!r} type")
    if not set(schema) <= _KEYWORDS[kind] | {"type", "description", "default"}:
        raise RejectedCatalog(f"property {name!r} has other keywords")
    if "description" in schema and not _short_text(
        schema["description"], _MAX_PROPERTY_DESCRIPTION
    ):
        raise RejectedCatalog(f"property {name!r} description")

    if kind == "array":
        items = schema.get("items")
        if not (
            isinstance(items, dict)
            and set(items) == {"type"}
            and items["type"] in _SCALAR_TYPES
        ):
            raise RejectedCatalog(f"property {name!r} items")
        if "maxItems" in schema and not _is_count(schema["maxItems"]):
            raise RejectedCatalog(f"property {name!r} maxItems")
        default = schema.get("default", [])
        if not (
            isinstance(default, list)
            and all(_is_of_type(item, items["type"]) for item in default)
        ):
            raise RejectedCatalog(f"property {name!r} default")
        return

    for bound in ("minimum", "maximum"):
        if bound in schema and not _is_of_type(schema[bound], "number"):
            raise RejectedCatalog(f"property {name!r} {bound}")
    for bound in ("minLength", "maxLength"):
        if bound in schema and not _is_count(schema[bound]):
            raise RejectedCatalog(f"property {name!r} {bound}")
    if "enum" in schema:
        enum = schema["enum"]
        if not (
            isinstance(enum, list)
            and 0 < len(enum) <= _MAX_ENUM
            and all(_is_of_type(item, kind) for item in enum)
        ):
            raise RejectedCatalog(f"property {name!r} enum")
    if "default" in schema and not _is_of_type(schema["default"], kind):
        raise RejectedCatalog(f"property {name!r} default")


def _check_input_schema(schema: object) -> dict[str, object]:
    """*schema* if it is inside the closed subset, else :class:`RejectedCatalog`.

    The subset: an object schema with ``properties``, ``required`` and
    ``additionalProperties: false``, whose properties are scalars or arrays of
    scalars with a handful of bounds. No ``$ref``, no nesting, nothing else.
    Arguments are not checked against it here; the remote end does that.
    """
    if not isinstance(schema, dict):
        raise RejectedCatalog("inputSchema is not an object")
    if set(schema) != {"type", "properties", "required", "additionalProperties"}:
        raise RejectedCatalog("inputSchema keys")
    if schema["type"] != "object" or schema["additionalProperties"] is not False:
        raise RejectedCatalog("inputSchema is not a closed object")
    properties = schema["properties"]
    if not isinstance(properties, dict) or len(properties) > _MAX_PROPERTIES:
        raise RejectedCatalog("properties")
    for name, prop in properties.items():
        _check_property(name, prop)
    required = schema["required"]
    if not (
        isinstance(required, list)
        and all(isinstance(name, str) and name in properties for name in required)
        and len(set(required)) == len(required)
    ):
        raise RejectedCatalog("required")
    return schema


def _check_annotations(name: str, raw: object) -> dict[str, bool | str]:
    """The allowed hints of *raw*; other keys are dropped, wrong types rejected."""
    if not isinstance(raw, dict):
        raise RejectedCatalog(f"{name} annotations")
    annotations: dict[str, bool | str] = {}
    for hint in _HINTS:
        if hint in raw:
            value = raw[hint]
            if not isinstance(value, bool):
                raise RejectedCatalog(f"{name} {hint}")
            annotations[hint] = value
    if "title" in raw:
        value = raw["title"]
        if not _short_text(value, _MAX_TITLE):
            raise RejectedCatalog(f"{name} annotation title")
        annotations["title"] = value
    return annotations


def _check_tool(entry: object) -> ToolSpec:
    if not isinstance(entry, dict):
        raise RejectedCatalog("tool is not an object")
    name = entry.get("name")
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise RejectedCatalog("tool name")
    description = entry.get("description")
    if not isinstance(description, str) or not _short_text(
        description, _MAX_DESCRIPTION
    ):
        raise RejectedCatalog(f"{name} description")
    title = entry.get("title")
    if title is not None and not _short_text(title, _MAX_TITLE):
        raise RejectedCatalog(f"{name} title")
    return ToolSpec(
        name=name,
        description=description,
        input_schema=_check_input_schema(entry.get("inputSchema")),
        title=title,
        annotations=_check_annotations(name, entry.get("annotations", {})),
    )


def admit(body: bytes, *, builtin: frozenset[str]) -> Catalog:
    """Parse and check a catalog body against the closed admission contract.

    All or nothing: any breach, including one tool whose name collides with a
    built-in tool, rejects the whole catalog. Fields outside the contract are
    dropped, so the contract can grow without old servers refusing it. Every
    string it keeps must be valid Unicode and every number finite.
    """
    if len(body) > _CATALOG_MAX_BYTES:
        raise RejectedCatalog("catalog is too large")
    try:
        document = _loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RejectedCatalog("catalog is not JSON") from exc
    if not isinstance(document, dict):
        raise RejectedCatalog("catalog is not an object")
    instructions = document.get("instructions", "")
    if not _short_text(instructions, _MAX_INSTRUCTIONS):
        raise RejectedCatalog("instructions")
    entries = document.get("tools")
    if not isinstance(entries, list):
        raise RejectedCatalog("tools")
    tools = tuple(_check_tool(entry) for entry in entries)
    names = [tool.name for tool in tools]
    if len(set(names)) != len(names):
        raise RejectedCatalog("duplicate tool names")
    if builtin.intersection(names):
        raise RejectedCatalog("a tool name collides with a built-in tool")
    return Catalog(instructions=instructions.strip(), tools=tools)


# --------------------------------------------------------------------------
# A call
# --------------------------------------------------------------------------


class Unavailable(Exception):
    """The remote answered nothing usable: no answer, a bad status, a bad body."""


@dataclass(frozen=True)
class CallResult:
    texts: tuple[str, ...]
    is_error: bool


def _call_result(body: bytes) -> CallResult:
    """The text items and error flag of a call answer; :class:`Unavailable` if invalid.

    A text item that is not valid Unicode makes the whole answer invalid,
    because the transport could not send it on. The size of the answer is
    bounded where it is read.
    """
    try:
        document = _loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise Unavailable("answer is not JSON") from exc
    if not isinstance(document, dict):
        raise Unavailable("answer is not an object")
    content = document.get("content")
    is_error = document.get("isError", False)
    if not isinstance(content, list) or not isinstance(is_error, bool):
        raise Unavailable("answer shape")
    texts = tuple(
        item["text"]
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    )
    if not all(_is_text(text) for text in texts):
        raise Unavailable("answer text is not valid Unicode")
    return CallResult(texts=texts, is_error=is_error)


def call(
    *,
    name: str,
    arguments: Mapping[str, object],
    client: str | None,
    transport: str,
    deadline: float,
    url: str | None = None,
) -> CallResult:
    """POST one tool call and return its text items. Blocking; run it in a thread.

    The request carries exactly the tool name and the arguments, nothing of
    the client's own request. Every socket operation gets the time left until
    *deadline*, and the answer is read in bounded chunks. A 200 or a 400 with
    a valid body is a result; anything else raises :class:`Unavailable`.
    """
    headers = {
        "User-Agent": user_agent(transport),
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if client is not None:
        headers["X-MCP-Client"] = client
    if language := accept_language():
        headers["Accept-Language"] = language
    data = json.dumps({"name": name, "arguments": dict(arguments)}).encode("utf-8")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise Unavailable("no time left")
    try:
        status, body = _request(
            url or call_url(),
            headers=headers,
            data=data,
            limit=_CALL_MAX_BYTES,
            timeout=remaining,
            deadline=deadline,
        )
    except (OSError, ValueError, http.client.HTTPException) as exc:
        raise Unavailable(type(exc).__name__) from exc
    if status not in (200, 400):
        raise Unavailable(f"status {status}")
    return _call_result(body)


def reset_for_testing() -> None:
    """Forget a started catalog request, as if :func:`prefetch` never ran."""
    global _pending
    _pending = None
