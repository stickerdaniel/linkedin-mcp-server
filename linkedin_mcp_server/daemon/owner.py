"""The process that holds the browser and serves it over loopback.

Started by a frontend and independent of it afterwards: it holds the daemon lock
for its lifetime, listens on an ephemeral loopback port, and drives the one
Chromium every client shares. It detaches with ``start_new_session`` on POSIX;
on Windows it breaks away from the starter's Job and adopts its own named Job
after COMMIT, before publishing.

Publication belongs to this lock holder. The child proves its endpoint and
writes only a pending descriptor; the frontend's COMMIT frame
(:mod:`linkedin_mcp_server.daemon.startup`) lets it replace the canonical one.
EOF before COMMIT aborts, parent loss after it cannot revoke the owner.

Every potentially blocking state access (the lock, the log) happens in this
process, so the frontend enforces its deadline by killing it. A timed-out thread
could still acquire the lock or publish later; a killed process cannot.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import os
import queue
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO, cast

import httpx2

from linkedin_mcp_server import __version__
from linkedin_mcp_server.bootstrap import (
    browser_setup_failure_pending,
    browser_setup_in_progress,
)
from linkedin_mcp_server.common_utils import is_still_at
from linkedin_mcp_server.config import set_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon import config as daemon_config
from linkedin_mcp_server.daemon import descriptor as daemon_descriptor
from linkedin_mcp_server.daemon import startup
from linkedin_mcp_server.daemon import version as daemon_version
from linkedin_mcp_server.daemon.core.owner import (
    CutOff,
    Drain,
    Effect,
    EndpointGone,
    Retirement,
    Stop,
)
from linkedin_mcp_server.daemon.lock import DaemonLock
from linkedin_mcp_server.daemon.liveness import (
    CALL_HEADER,
    HEARTBEAT_PATH,
    RETIRING,
    call_id_in,
    get_liveness,
)
from linkedin_mcp_server.drivers.browser import set_headless
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_mcp_server.private_state import (
    PrivateStateError,
    harden_created_file,
    harden_file,
)
from linkedin_mcp_server.process_tree import WindowsJob
from linkedin_mcp_server.server_role import (
    ServerRole,
    hard_exit_required,
    set_process_role,
    stand_down_reason,
)
from linkedin_mcp_server.session_state import auth_root_dir, get_runtime_id

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

#: Bounded bootstrap records carried on the original standard error. Stage
#: records contain no values; the post-attach hint carries only its nonce and log path.
BOOTSTRAP_PREFIX = "daemon-bootstrap:"
BOOTSTRAP_CONFIGURATION = "configuration"
BOOTSTRAP_STATE = "state"
BOOTSTRAP_LOCK = "lock"
BOOTSTRAP_LOG = "log"
BOOTSTRAP_ATTACHED = "attached"
BOOTSTRAP_LOG_HINT_PREFIX = "daemon-bootstrap-log:"
BOOTSTRAP_LOG_HINT_VERSION = "1"
BOOTSTRAP_LOG_HINT_MAX_PATH_BYTES = 2048

#: Where the owner serves MCP. Fixed rather than configurable: the frontend
#: reads it out of the descriptor, and the only thing a second value would do is
#: give two installations a way to disagree.
MCP_PATH = "/mcp"

#: How long the owner has to get from a bound socket to a proved endpoint.
#: Generous, because it covers the first import of the whole server graph on a
#: cold page cache. Publication gets its own bounded window afterwards: an
#: endpoint that proves itself near this boundary still deserves a chance to be
#: validated and committed.
_STARTUP_PROBE_SECONDS = 30.0
_COMMIT_AUTH_SECONDS = 30.0
_WINDOWS_REPLACE_RETRY_SECONDS = 0.01
_WINDOWS_RETRYABLE_REPLACE_ERRORS = frozenset({5, 32, 33})

_LOG_FILE = "daemon.log"


def daemon_log_path(auth_root: Path) -> Path:
    """Where a detached owner's output goes."""
    return daemon_descriptor.daemon_dir(auth_root) / _LOG_FILE


def _publish_windows_daemon_log(log_path: Path) -> None:
    """Publish an empty private log without exposing an in-progress final path."""
    try:
        log_path.lstat()
    except FileNotFoundError:
        pass
    else:
        harden_file(log_path)
        return

    descriptor, staged_name = tempfile.mkstemp(
        prefix=".daemon-log-", dir=log_path.parent
    )
    staged = Path(staged_name)
    os.close(descriptor)
    published = False
    try:
        harden_created_file(staged)
        # Never an overwriting replace: an entry that appeared since the check
        # above is foreign, and the rename refuses it.
        staged.rename(log_path)
        published = True
        harden_file(log_path)
    finally:
        # A published log is left where it is, even when the verification after
        # it failed. Once published, the path no longer proves which file it
        # names, so removing it could take a foreign entry with it; an empty
        # file hardened before publication is the cheaper thing to leave.
        if not published:
            with contextlib.suppress(OSError):
                staged.unlink()


def _attach_daemon_log(auth_root: Path) -> Path:
    """Open the daemon log inside the child and attach both output streams.

    May block on unavailable state storage, so it runs only in the owner
    process, which the frontend can hard-kill. Runs only under the daemon lock,
    so no cooperating owner creates this file at the same time: an entry that
    appears between the check and the creation is foreign and fails startup.
    """
    directory = daemon_descriptor.prepare_daemon_state(auth_root)
    log_path = directory / _LOG_FILE
    created = False
    if _IS_WINDOWS:
        _publish_windows_daemon_log(log_path)
    else:
        try:
            log_path.lstat()
        except FileNotFoundError:
            created = True
        else:
            # The directory only became private above. An entry planted before then
            # has to prove its own owner and access before open can append to it.
            harden_file(log_path)

    flags = (
        os.O_APPEND
        | os.O_WRONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    if created:
        flags |= os.O_CREAT | os.O_EXCL
    descriptor = os.open(log_path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PrivateStateError(f"{log_path} is not a regular file")
        if created:
            harden_created_file(log_path)
        if not is_still_at(descriptor, log_path):
            raise PrivateStateError(
                f"{log_path} was replaced while its private access was being "
                f"established"
            )
        os.dup2(descriptor, sys.stdout.fileno())
        os.dup2(descriptor, sys.stderr.fileno())
    finally:
        # No removal on failure, for the reason _publish_windows_daemon_log
        # gives: the path may no longer name this inode, and an owner-only
        # empty file costs nothing to leave.
        os.close(descriptor)
    return log_path


def _bind_loopback() -> socket.socket:
    """Bind an ephemeral loopback port, and return the listening socket.

    Bound here rather than by uvicorn because the descriptor needs the port,
    which the kernel settles only at bind time. IPv6 first, then IPv4 for hosts
    with IPv6 disabled.
    """
    for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
        sock: socket.socket | None = None
        try:
            # Inside the guard with the bind: a kernel built without IPv6
            # refuses the family here rather than at the bind, and that is the
            # same answer, not a different failure.
            sock = socket.socket(family, socket.SOCK_STREAM)
            # Deliberately no SO_REUSEADDR. On an ephemeral port there is
            # nothing to reuse, and on the BSDs, macOS among them, it would let
            # a second bind succeed against a live listener on some
            # configurations. A port the kernel just handed out is unused.
            sock.bind((host, 0))
            sock.listen(128)
        except OSError as exc:
            if sock is not None:
                sock.close()
            if family is socket.AF_INET6 and exc.errno in (
                errno.EAFNOSUPPORT,
                errno.EADDRNOTAVAIL,
                errno.EPROTONOSUPPORT,
            ):
                continue
            raise
        return sock
    raise OSError("No loopback address could be bound for the daemon endpoint")


def _endpoint_host(sock: socket.socket) -> str:
    """The address to publish for *sock*, unbracketed.

    The descriptor accepts only the bare literal and brackets it itself when it
    builds a URL, so a bracketed host here would be refused by every client.
    """
    address = sock.getsockname()[0]
    return str(address)


def direct_http_client(*, timeout: float) -> httpx2.Client:
    """An HTTP client that talks to this machine and nowhere else.

    Every daemon request carries the owner's bearer token. httpx2 honours
    ``HTTP_PROXY`` even for ``127.0.0.1``, which would hand that token to a
    proxy, so ``trust_env=False`` refuses proxies (and ``.netrc`` and
    environment certificates) outright. The user's proxy is for LinkedIn's
    traffic only.
    """
    return httpx2.Client(trust_env=False, timeout=timeout)


def direct_async_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx2.Timeout | None = None,
    auth: httpx2.Auth | None = None,
    **extra: Any,
) -> httpx2.AsyncClient:
    """The asynchronous counterpart, shaped as FastMCP's client factory.

    The named parameters are the SDK's ``McpHttpClientFactory`` protocol.
    ``**extra`` is required too: FastMCP's transport also passes
    ``follow_redirects``, and a factory without it fails at connect time.
    ``trust_env`` is the one thing a caller may not override.
    """
    extra.pop("trust_env", None)
    return httpx2.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else httpx2.Timeout(30.0),
        auth=auth,
        trust_env=False,
        **extra,
    )


async def prove_hop(url: str, token: str) -> None:
    """Prove the endpoint serves the hop a frontend forwards over, or raise.

    An authenticated MCP round trip, because a TCP connect succeeds before any
    route is mounted. A tool listing rather than a ping: protocol 2026-07-28
    removed ping, and listing touches no browser. Negotiated as the forwarder
    negotiates (``forwarder.pin_hop_version``): with the SDK's default, an
    endpoint whose dependencies no longer serve the pinned version answered
    through an older era and then failed every call. The owner asks this of
    itself before publishing; a frontend asks it at discovery.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from linkedin_mcp_server.daemon.forwarder import pin_hop_version

    async with (
        # Never through a proxy: this is a loopback hop carrying the token to a
        # logged-in session. See ``direct_http_client``.
        direct_async_http_client(headers={"Authorization": f"Bearer {token}"}) as http,
        streamable_http_client(url, http_client=http) as (read, write),
        ClientSession(read, write) as session,
    ):
        await pin_hop_version(session)
        await session.list_tools()


#: The route a frontend uses to ask an owner to stand down, with the bearer
#: token and a stand-down request body (:func:`stand_down_request`). The one
#: interface a later release uses to retire this one: its path, its bearer check
#: and request version 1 stay as they are across tool protocol and descriptor
#: schema changes, so it is also all a frontend may do with an owner whose tool
#: protocol it does not speak (``daemon.discovery.Attachment.control_only``).
STAND_DOWN_PATH = "/control/stand-down"

#: The version of the stand-down request body, independent of the tool protocol.
STAND_DOWN_REQUEST_VERSION = 1

#: Close admission and drain, whatever is in flight: a newer build takes over.
STAND_DOWN_TURNOVER = "turnover"

#: Retire only if nothing is in flight or queued: a confirmed profile command.
STAND_DOWN_IDLE_ONLY = "idle_only"

_STAND_DOWN_MODES = frozenset({STAND_DOWN_TURNOVER, STAND_DOWN_IDLE_ONLY})
_STAND_DOWN_KEYS = frozenset({"version", "mode", "instance"})
#: A request is about a hundred bytes; anything far larger is not one.
_STAND_DOWN_BODY_LIMIT = 1024


def _matches_token(presented: str, expected: str) -> bool:
    """Whether a presented bearer token is the expected one.

    Compared through digests on purpose: ``hmac.compare_digest`` raises
    ``TypeError`` on a non-ASCII string, which would turn an unauthenticated
    header into a 500, and digests hide the real token's length.
    """
    return hmac.compare_digest(
        hashlib.sha256(presented.strip().encode("utf-8", "surrogatepass")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def stand_down_request(instance_id: str, *, mode: str) -> dict[str, Any]:
    """The body that asks the owner *instance_id* to stand down in *mode*."""
    if mode not in _STAND_DOWN_MODES:
        raise ValueError(f"Unknown stand-down mode: {mode!r}")
    return {
        "version": STAND_DOWN_REQUEST_VERSION,
        "mode": mode,
        "instance": instance_id,
    }


def _unique_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """An object whose members are named once, refused otherwise.

    ``json.loads`` keeps the last of two equal names, so a body naming
    ``mode`` twice would be judged on whichever came second.
    """
    payload = dict(pairs)
    if len(payload) != len(pairs):
        raise ValueError("duplicate member")
    return payload


def _stand_down_mode(body: bytes, instance_id: str | None) -> str | None:
    """The mode of *body* if it is exactly a request addressed to this owner.

    Anything looser, an empty body included, changes nothing. Types are checked
    with ``type() is`` because a bool is an ``int``.
    """
    try:
        payload = json.loads(body, object_pairs_hook=_unique_members)
    except ValueError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.keys() != _STAND_DOWN_KEYS
        or type(payload["version"]) is not int
        or payload["version"] != STAND_DOWN_REQUEST_VERSION
        or type(payload["mode"]) is not str
        or payload["mode"] not in _STAND_DOWN_MODES
        or instance_id is None
        or payload["instance"] != instance_id
    ):
        return None
    return payload["mode"]


def create_owner_server(
    *,
    config: AppConfig,
    token: str,
    host: str,
    port: int,
    stand_down: Callable[[], None] | None = None,
) -> Any:
    """Build the authenticated HTTP server the owner runs.

    Uvicorn directly rather than ``FastMCP.run_http_async``, which binds its own
    port and blocks without a handle. This process needs the port before it
    publishes, the started flag before it probes, and a handle to stop with.
    """
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from linkedin_mcp_server.server import create_mcp_server

    mcp = create_mcp_server(
        tool_timeout=config.server.tool_timeout_seconds,
        role=ServerRole.OWNER,
        auth_token=token,
    )

    if stand_down is not None:

        @mcp.custom_route(STAND_DOWN_PATH, methods=["POST"])
        async def stand_down_route(request: Request) -> JSONResponse:
            """Give up the browser so a newer build can take over.

            In idle-only mode, for a confirmed profile command, only if nothing
            is in flight or queued; a busy owner answers 409 with nothing
            changed.

            FastMCP mounts custom routes outside its authentication middleware,
            so the token is checked here; without it any local process or web
            page could stop the shared browser.
            """
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not _matches_token(presented, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            # Read whole and judged before anything changes. A body this owner
            # cannot read exactly, an empty one included, changes nothing. Read
            # under a cap rather than with request.body(), which buffers
            # whatever arrives, chunked or not.
            body = bytearray()
            async for chunk in request.stream():
                body += chunk
                if len(body) > _STAND_DOWN_BODY_LIMIT:
                    return JSONResponse(
                        {"error": "unsupported stand-down request"}, status_code=400
                    )
            liveness = get_liveness()
            mode = _stand_down_mode(bytes(body), liveness.instance_id)
            if mode is None:
                return JSONResponse(
                    {"error": "unsupported stand-down request"}, status_code=400
                )
            if mode == STAND_DOWN_TURNOVER:
                stand_down()
                return JSONResponse(
                    {"standing_down": True, "instance": liveness.instance_id}
                )
            # A profile command the user confirmed, asking only if nobody else
            # would lose a call. Nothing awaits from the check to the reply, so a
            # call cannot be admitted between the verdict and the retirement: it
            # is either counted before and makes this busy, or refused after.
            # Busy is asked first and on its own: an owner already retiring for
            # another reason still has calls it is draining, and the user agreed
            # only to retiring an owner nobody is using.
            setup = browser_setup_in_progress()
            if liveness.busy(background_work=setup) or not liveness.try_retire(
                Retirement.PROFILE_COMMAND, background_work=setup
            ):
                return JSONResponse(
                    {"standing_down": False, "busy": True}, status_code=409
                )
            stand_down()
            return JSONResponse(
                {
                    "standing_down": True,
                    "retiring": True,
                    "instance": liveness.instance_id,
                }
            )

    @mcp.custom_route(HEARTBEAT_PATH, methods=["POST"])
    async def heartbeat_route(request: Request) -> JSONResponse:
        """Note that somebody is still waiting for a call this owner is running.

        Checks the token itself, like the stand-down route. An unknown call
        gets ``watched: false``: a late beat or a preflight, not a fault. Once
        the owner would refuse a call, unpublished or retiring, an unknown call
        gets a signed 409 so the frontend learns before dispatching that it
        would be refused; an admitted call keeps getting 200 while it runs.
        """
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not _matches_token(presented, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        call_id = call_id_in(request.headers.get(CALL_HEADER))
        if call_id is None:
            return JSONResponse({"error": "no call named"}, status_code=400)
        liveness = get_liveness()
        if liveness.heard(call_id):
            return JSONResponse({"watched": True})
        # The same readiness admission checks, so a preflight that passes is
        # not followed by a refused call.
        if not liveness.admitting:
            return JSONResponse(
                {
                    "daemon": RETIRING,
                    "protocol": daemon_descriptor.PROTOCOL_VERSION,
                    "instance": liveness.instance_id,
                },
                status_code=409,
            )
        return JSONResponse({"watched": False})

    app = mcp.http_app(
        path=MCP_PATH,
        transport="http",
        # The same defence the documented HTTP bind gets. A loopback listener is
        # reachable from every process on the machine, and from a browser the
        # user merely points at a page: a website could otherwise drive this
        # server through the user's own browser, from inside the network where a
        # firewall does not help. The token is the real barrier, and a Host this
        # server does not answer to is still worth refusing before it is checked.
        host_origin_protection=True,
        allowed_hosts=[host, f"{host}:{port}", f"[{host}]", f"[{host}]:{port}"],
    )
    return uvicorn.Server(
        uvicorn.Config(
            app,
            log_level=config.server.log_level.lower(),
            lifespan="on",
            timeout_graceful_shutdown=5,
        )
    )


class _CommitPublicationUnpublished(RuntimeError):
    """A bounded replacement retry proved this generation was not published."""


def _retryable_windows_replace(exc: OSError) -> bool:
    return os.name == "nt" and getattr(exc, "winerror", None) in (
        _WINDOWS_RETRYABLE_REPLACE_ERRORS
    )


def _commit_definitely_failed(exc: BaseException) -> bool:
    return isinstance(
        exc, _CommitPublicationUnpublished
    ) or daemon_descriptor.replace_definitely_failed(exc)


async def _commit_prepared_until(
    auth_root: Path, instance_id: str, deadline: float
) -> None:
    """Commit after transient Windows descriptor readers release their handles."""
    while True:
        try:
            # Kept in the lock-holding thread deliberately. An abandoned worker
            # could complete the replacement after this owner released the lock
            # and a successor started. A hard-mounted remote home can therefore
            # pin this child in the kernel; bounded parent reads preserve frontend
            # fallback, and #796 tracks moving coordination state to local storage.
            daemon_descriptor.commit_prepared(auth_root, instance_id)
            return
        except Exception as exc:
            if not (isinstance(exc, OSError) and _retryable_windows_replace(exc)):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # MoveFileEx returned false for every one of these attempts. Access
                # and sharing violations are retryable because readers may release
                # their handles, but the failed calls did not publish anything.
                raise _CommitPublicationUnpublished(
                    "Descriptor replacement stayed blocked until its deadline"
                ) from exc
            await asyncio.sleep(min(_WINDOWS_REPLACE_RETRY_SECONDS, remaining))


class StartupControl(Protocol):
    """Where the frontend's COMMIT arrives: one blocking frame read."""

    def read(self) -> dict[str, object]: ...


async def _read_control_until(
    control: StartupControl, deadline: float
) -> dict[str, object]:
    """Read one frontend frame without making event-loop shutdown wait on it."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[dict[str, object]] = loop.create_future()

    def read() -> None:
        try:
            decision = control.read()
        except BaseException as exc:  # noqa: BLE001 - delivered to the event loop

            def fail(error: BaseException = exc) -> None:
                if not result.done():
                    result.set_exception(error)

            complete = fail
        else:

            def succeed(value: dict[str, object] = decision) -> None:
                if not result.done():
                    result.set_result(value)

            complete = succeed
        with contextlib.suppress(RuntimeError):
            # A lambda, because ty cannot solve call_soon_threadsafe's *args
            # against a union of callbacks whose parameters have defaults.
            loop.call_soon_threadsafe(lambda: complete())

    threading.Thread(target=read, name="daemon-control", daemon=True).start()
    return await asyncio.wait_for(result, max(deadline - time.monotonic(), 0.0))


def _authorizes(decision: object, handshake_nonce: str, instance_id: str) -> bool:
    """Whether *decision* is the frontend's COMMIT for this prepared generation."""
    try:
        commit = startup.expect(decision, startup.COMMIT, nonce=handshake_nonce)
    except startup.ProtocolError:
        return False
    return commit["instance"] == instance_id


async def _serve(
    *,
    lock: DaemonLock,
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    log_path: Path,
    handshake: Handshake,
    handshake_nonce: str,
    control: StartupControl,
    job_name: str | None = None,
) -> int:
    """Run the endpoint, prepare it, and serve only after parent commit."""
    instance_id = daemon_descriptor.new_instance_id()
    token = daemon_descriptor.new_token()

    sock = _bind_loopback()
    host, port = _endpoint_host(sock), sock.getsockname()[1]

    # Before the server exists, so every refusal it can give names this owner.
    get_liveness().serving_as(instance_id)

    server = create_owner_server(
        config=config,
        token=token,
        host=host,
        port=port,
        stand_down=get_liveness().turn_over,
    )

    descriptor = daemon_descriptor.build(
        instance_id=instance_id,
        package_version=__version__,
        runtime_id=get_runtime_id(),
        profile=profile,
        host=host,
        port=port,
        path=MCP_PATH,
        token=token,
        config=config,
        log_path=log_path,
    )

    serving = asyncio.create_task(server.serve(sockets=[sock]), name="daemon-endpoint")
    try:
        # One budget across both halves of startup, not one each. They are two
        # stages of the same thing from the frontend's side, and it waits on the
        # total: an allowance each let an owner take twice what the frontend
        # would ever wait for, so it could be following its own rules and still
        # be written off as silent and stopped.
        startup_deadline = time.monotonic() + _STARTUP_PROBE_SECONDS
        await _await_started(server, serving, timeout=_STARTUP_PROBE_SECONDS)
        # Built before it is proved, and proved through its own ``url``: a probe
        # against a URL assembled here would pass while the one clients actually
        # use was malformed. An IPv6 endpoint is exactly that case, since the
        # literal has to be bracketed.
        await asyncio.wait_for(
            prove_hop(descriptor.url, token),
            max(startup_deadline - time.monotonic(), 0.0),
        )

        commit_deadline = time.monotonic() + _COMMIT_AUTH_SECONDS
        _forget_superseded_tokens(auth_root)
        daemon_descriptor.prepare(auth_root, descriptor, token)
        handshake.prepared(instance_id)

        # Failed starts deliberately leave this generation's unique pending files.
        # Removing them re-enters account-home storage and can pin this process in
        # the kernel while it still owns the lock. The next successful lock holder's
        # pre-publication sweep removes every superseded generation instead.
        try:
            decision: object = await _read_control_until(control, commit_deadline)
        except TimeoutError:
            logger.warning("The daemon starter did not authorize commit in time")
            handshake.retry()
            server.should_exit = True
            await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
            return 1
        except (EOFError, OSError, ValueError):
            # End of file is the frontend's abort, and anything it cannot have
            # written means the same: nothing authorized this generation.
            decision = None
        if not _authorizes(decision, handshake_nonce, instance_id):
            logger.info("The daemon starter exited before committing this owner")
            handshake.abort()
            server.should_exit = True
            await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
            return 0

        # The parent retains termination authority until this exact COMMIT frame.
        # Adopt before the first replacement attempt so parent loss cannot kill an
        # owner whose publication may already have happened on remote storage.
        if os.name == "nt":
            if job_name is None:
                raise RuntimeError("The Windows owner has no Job Object handoff")
            try:
                WindowsJob.adopt_current_process(job_name)
            except Exception:
                logger.exception("The daemon could not adopt its Windows Job Object")
                handshake.abort()
                server.should_exit = True
                await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
                return 1

        try:
            await _commit_prepared_until(auth_root, instance_id, commit_deadline)
        except BaseException as exc:
            # A preflight failure means replacement was never attempted, so it is
            # terminal without another state read.
            if _commit_definitely_failed(exc):
                logger.exception("Daemon descriptor publication failed definitively")
                handshake.abort()
                server.should_exit = True
                await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
                raise
            # An error from the replacement itself may arrive after an NFS server
            # applied the rename, so the canonical descriptor may already name
            # this endpoint. Admission closes before the first yield: a frontend
            # that reads it is refused with a signed answer and looks for a
            # successor. FAILED, never ABORTED, which would claim nothing was
            # published; the frontend settles from canonical state. No retry,
            # and the token stays for ordinary successor handling.
            get_liveness().retire(Retirement.UNCERTAIN_PUBLICATION)
            # The log sits on the same storage that just stalled the rename, so
            # the watchdog bounds every synchronous write, and FAILED goes out
            # before them. The process ends inside the guard, even after a
            # clean or cancelled stop: anything later, such as `main` logging
            # the error, would run with the lock held and nothing bounding it.
            with _hard_exit_after(_FAILED_STARTUP_SHUTDOWN_SECONDS, lock):
                handshake.fail()
                logger.warning(
                    "Daemon descriptor publication is ambiguous; exiting without serving",
                    exc_info=True,
                )
                server.should_exit = True
                try:
                    await _stop_within(
                        serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock
                    )
                finally:
                    _exit_hard(lock)

        # Publication is now proved. Only in-memory startup state and the verdict
        # follow, so a blocked log or state mount cannot strand a canonical owner
        # before the frontend learns it is ready. No await separates the commit
        # from admission opening, so no request is served in between.
        get_liveness().the_endpoint_is_live()
        handshake.committed()
    except BaseException:
        server.should_exit = True
        # Through the same bounded stop as the stand-down path, and for the same
        # reason: `wait_for` waits for the cancellation it requested to finish,
        # so a task that suppresses cancellation makes this line unbounded no
        # matter what timeout it is given. `suppress` only helps once the await
        # returns. This process holds the daemon lock by now, so a failed
        # startup that never exits is a profile nothing can elect an owner for.
        await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
        raise

    # Held for the process lifetime, and handed down rather than dropped: the
    # one path that skips interpreter cleanup frees it itself (`_exit_hard`).
    await _serve_until_stopped(
        server, serving, config.browser.browser_idle_timeout_seconds, lock=lock
    )
    return 0


#: How often the owner checks whether it has been asked to stand down. A
#: turnover happens once per upgrade, so a leisurely poll costs a user nothing
#: and keeps the request itself free of any dependency on the serving loop.
_STAND_DOWN_POLL_SECONDS = 0.1

#: How long a standing-down owner may take to shut down before it exits anyway.
#: Comfortably past uvicorn's own five second connection grace, so an ordinary
#: shutdown is never cut short, and short enough that a hung one does not hold
#: the lock for a user's whole session.
_STAND_DOWN_SHUTDOWN_SECONDS = 30.0

#: The same bound for a startup that failed. Shorter, because nothing is being
#: preserved: admission never opened, so no call ran on this owner, even when an
#: ambiguous commit may have published its descriptor.
_FAILED_STARTUP_SHUTDOWN_SECONDS = 10.0


async def _serve_until_stopped(
    server: Any,
    serving: asyncio.Task[None],
    idle_timeout: float = 0.0,
    *,
    lock: DaemonLock | None,
) -> None:
    """Hold the endpoint open until it stops, or until the owner should go.

    Each turn hands what this process observes to the lifecycle core
    (``daemon.core.owner``), which decides to serve, expire abandoned calls,
    drain or stop. A stand-down is acted on here, not where it arrives, so its
    caller gets a reply before this process exits.

    The descriptor is left behind on every way out: the next election probes
    and replaces it, while deleting it here would race whoever publishes next.
    """
    liveness = get_liveness()
    while True:
        effects = liveness.tick(
            idle_timeout=idle_timeout,
            wedged=stand_down_reason(),
            setup_running=browser_setup_in_progress(),
            setup_failed=browser_setup_failure_pending(),
            endpoint_stopped=serving.done(),
        )
        ending = _carry_out_now(effects)
        if ending is not None:
            await _carry_out_ending(ending, server, serving, lock=lock)
            return
        await asyncio.sleep(_STAND_DOWN_POLL_SECONDS)


def _carry_out_now(effects: list[Effect]) -> Effect | None:
    """Run the effects that need no wait, in order, and return the ending, if any.

    The core puts the ending last, so everything decided before it, a cut
    call's cancellation included, has run by the time this returns.
    """
    liveness = get_liveness()
    for effect in effects:
        match effect:
            case Drain(requested_by=Retirement.PROFILE_COMMAND):
                logger.info("A profile command asked for the browser; standing down")
            case Drain():
                logger.info("A newer build asked for the browser; standing down")
            case CutOff():
                # Neutral on purpose: a call still queued is answered as not
                # run, and only one whose body began as an unknown outcome.
                logger.warning(
                    "Requested stand-down cancellation of %d pending call(s)",
                    effect.count,
                )
            case Stop() | EndpointGone():
                return effect
            case _:
                liveness.carry_out(effect)
    return None


async def _carry_out_ending(
    ending: Effect,
    server: Any,
    serving: asyncio.Task[None],
    *,
    lock: DaemonLock | None,
) -> None:
    """Stop the endpoint the way the core decided, or wait out one that stopped."""
    if isinstance(ending, EndpointGone):
        try:
            await serving
        finally:
            if hard_exit_required():
                logger.error(
                    "Browser shutdown was not confirmed; exiting hard so the "
                    "profile stays held until the browser is gone"
                )
                _exit_hard(lock)
        return
    assert isinstance(ending, Stop)
    if ending.because is Retirement.WEDGED:
        logger.warning("Standing down: %s", ending.wedged)
    elif ending.because is Retirement.IDLE:
        logger.info("Nothing has needed the browser in %.0fs; exiting", ending.quiet)
    server.should_exit = True
    # Graceful, but not unconditionally. Uvicorn finishes the requests in
    # flight, a cut call's answer included, and runs the lifespan, which closes
    # the browser, and only then does this process exit and the kernel free the
    # lock.
    #
    # Bounded, because that shutdown is not guaranteed to finish:
    # ``timeout_graceful_shutdown`` bounds the connection tasks and nothing
    # bounds the lifespan behind them. An owner stuck there would hold the
    # daemon lock forever, having perhaps already promised a frontend that it
    # was standing down. Giving up the wait and exiting is the lesser harm, and
    # that exit frees the election before it ends the process (`_exit_hard`).
    await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS, lock=lock)


async def _stop_within(
    serving: asyncio.Task[None], seconds: float, *, lock: DaemonLock | None
) -> None:
    """Let the server finish shutting down, and exit hard if it will not.

    Returning on timeout is not enough: ``asyncio.run`` would then wait without
    bound for the serving task's cancellation, and ``close_browser`` holds
    cancellation back until teardown finishes, so a stuck browser would keep
    the daemon lock forever. *lock* is passed so :func:`_exit_hard` frees the
    election first.
    """
    try:
        await asyncio.wait_for(asyncio.shield(serving), seconds)
        if hard_exit_required():
            logger.error(
                "Browser shutdown was not confirmed; exiting hard so the "
                "profile stays held until the browser is gone"
            )
            _exit_hard(lock)
        return
    except TimeoutError:
        pass
    except Exception:
        logger.warning("The daemon endpoint stopped with an error", exc_info=True)
        if hard_exit_required():
            _exit_hard(lock)
        return

    _exit_hard(lock)


@contextlib.contextmanager
def _hard_exit_after(seconds: float, lock: DaemonLock | None) -> Iterator[None]:
    """Exit hard from a timer thread unless the block finishes within *seconds*.

    Bounds synchronous work no ``wait_for`` can interrupt, such as a log write
    to a stalled mount.
    """
    watchdog = threading.Timer(seconds, _exit_hard, args=(lock,))
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        watchdog.cancel()


def _exit_hard(lock: DaemonLock | None) -> NoReturn:
    """Leave immediately, without interpreter cleanup and without a signal.

    The browser ends as it does when a Direct host quits: through the crash
    guardian, which holds its own profile lease, on POSIX, and the per-launch
    kill-on-close Jobs on Windows. Only the election lock is released first, so
    a successor can contend; nothing else runs, and even a release that raises
    still ends in ``os._exit``. Idempotent, so ``main``'s ``finally`` may still
    release.
    """
    try:
        if lock is not None:
            lock.release_for_exit()
    finally:
        os._exit(1)


async def _await_started(
    server: object,
    serving: asyncio.Task[None],
    *,
    timeout: float = _STARTUP_PROBE_SECONDS,
) -> None:
    """Wait until uvicorn reports it is serving, or raise.

    Polls the ``started`` flag, the only signal uvicorn exposes, and watches
    the task so a server that died fails at once. Bounded because a hung
    lifespan would otherwise hold the daemon lock without ever serving.
    """
    deadline = time.monotonic() + timeout
    while not getattr(server, "started", False):
        if serving.done():
            # Re-raises whatever killed it, or reports the silent exit.
            await serving
            raise RuntimeError("The daemon endpoint stopped before it started serving")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"The daemon endpoint did not start within {timeout:.0f}s"
            )
        await asyncio.sleep(0.02)


def _forget_superseded_tokens(auth_root: Path) -> None:
    """Remove superseded tokens and abandoned pending files before preparing.

    The caller holds the lock, so the canonical descriptor can only name the
    generation about to be replaced, and its token stays. Runs before
    ``prepared``, while the frontend can still kill a child blocked on storage.
    """
    try:
        canonical = daemon_descriptor.read(auth_root)
        directory = daemon_descriptor.daemon_dir(auth_root)
        entries = list(directory.iterdir())
    except (OSError, daemon_descriptor.DescriptorError):
        logger.debug("Could not inspect old daemon state", exc_info=True)
        return
    keep = (
        None
        if canonical is None
        else daemon_descriptor.token_path(auth_root, canonical.instance_id).name
    )
    for entry in entries:
        superseded_token = entry.name.startswith("token-") and entry.name != keep
        abandoned_pending = entry.name.startswith("pending-")
        if superseded_token or abandoned_pending:
            with contextlib.suppress(OSError):
                entry.unlink()


class _BootstrapDiagnostics:
    """Bounded startup diagnostics kept separate from the daemon log."""

    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream

    def report(self, code: str) -> None:
        self._write((f"{BOOTSTRAP_PREFIX} {code}\n",))

    def attached(self, log_path: Path, handshake_nonce: str) -> None:
        """Report the attached stage, then optionally identify the opened log."""
        records = [f"{BOOTSTRAP_PREFIX} {BOOTSTRAP_ATTACHED}\n"]
        candidate = str(log_path)
        encoded = candidate.encode("utf-8", "surrogatepass")
        if (
            log_path.is_absolute()
            and log_path.name == _LOG_FILE
            and len(encoded) <= BOOTSTRAP_LOG_HINT_MAX_PATH_BYTES
            and all(character.isprintable() for character in candidate)
        ):
            records.append(
                f"{BOOTSTRAP_LOG_HINT_PREFIX} {BOOTSTRAP_LOG_HINT_VERSION} "
                f"{handshake_nonce} {candidate}\n"
            )
        self._write(records)

    def _write(self, records: tuple[str, ...] | list[str]) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            for record in records:
                stream.write(record)
                stream.flush()
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


class Handshake(Protocol):
    """How this process reports both stages of committed startup."""

    def prepared(self, instance_id: str) -> None: ...

    def committed(self) -> None: ...

    def fail(self) -> None: ...

    def abort(self) -> None: ...

    def retry(self) -> None: ...

    def close(self) -> None: ...


class _Handshake:
    """The frames this process answers the frontend with, after its ACK.

    A pipe, so an owner that dies before a verdict ends the frontend's read at
    once instead of being waited out.
    """

    def __init__(self, channel: startup.OwnerChannel | None, nonce: str) -> None:
        self._channel = channel
        self._nonce = nonce

    def prepared(self, instance_id: str) -> None:
        self._send(startup.PREPARED, close=False, instance=instance_id)

    def committed(self) -> None:
        self._send(startup.VERDICT, status=startup.COMMITTED)

    def fail(self) -> None:
        self._send(startup.VERDICT, status=startup.FAILED)

    def abort(self) -> None:
        self._send(startup.VERDICT, status=startup.ABORTED)

    def retry(self) -> None:
        self._send(startup.VERDICT, status=startup.RETRY)

    def _send(self, kind: str, *, close: bool = True, **fields: object) -> None:
        channel = self._channel
        if channel is None:
            return
        try:
            channel.send(kind, self._nonce, **fields)
        except (OSError, ValueError):
            pass
        finally:
            if close:
                self.close()

    def close(self) -> None:
        """End the output without a verdict, so the frontend sees the pipe end."""
        channel, self._channel = self._channel, None
        if channel is not None:
            channel.close_output()


def _claim_bootstrap_stream() -> TextIO | None:
    """Keep a private copy of standard error until the daemon log is attached."""
    try:
        duplicate = os.dup(sys.stderr.fileno())
    except (OSError, ValueError, AttributeError):
        return None
    return os.fdopen(duplicate, "w", encoding="utf-8")


def _claim_startup_channel() -> startup.OwnerChannel | None:
    """Take standard input and output for the startup protocol.

    The frontend reads only frames there and stops reading once answered, so
    stray output would end the exchange or later block on a full pipe.
    :meth:`startup.OwnerChannel.claim` keeps private copies and points the
    originals elsewhere.
    """
    try:
        return startup.OwnerChannel.claim()
    except (OSError, ValueError, AttributeError):
        # No usable standard streams means the required exchange is unavailable.
        # Startup fails before serving, because only the frontend can validate
        # and authorize this owner's prepared descriptor.
        return None


def _close_startup_channel(channel: startup.OwnerChannel | None) -> None:
    """End the exchange without making any startup claim."""
    if channel is not None:
        with contextlib.suppress(OSError, ValueError):
            channel.close()


def _take_lock(auth_root: Path) -> DaemonLock | None:
    """Hold the daemon lock, or report that another process does."""
    lock = DaemonLock(auth_root)
    # Losing is an ordinary race: another child is coming up, and the frontend
    # waits for whichever one publishes.
    if not lock.try_acquire():
        return None
    return lock


def main(argv: list[str] | None = None) -> int:
    """Run as the detached owner. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="linkedin-mcp-daemon",
        description="Serve one shared LinkedIn browser to local MCP clients.",
    )
    parser.add_argument("--job-name", default=None)
    args = parser.parse_args(argv)

    # Standard input and output carry the startup protocol. A private duplicate
    # of standard error carries one fixed, non-secret diagnosis until the real
    # log is ready. The nonce authenticating every later frame arrives in the
    # HELLO, so a failure before that read closes the output without a frame.
    bootstrap = _BootstrapDiagnostics(_claim_bootstrap_stream())
    channel = _claim_startup_channel()
    try:
        if os.name == "nt":
            if args.job_name is None:
                raise RuntimeError("The Windows owner has no Job Object handoff")
            WindowsJob.verify_current_process(args.job_name)
        # Before anything else touches the configuration, which arrives only
        # after this process has answered HELLO with its own build.
        handover = _read_handover(channel)
    except BaseException:
        bootstrap.report(BOOTSTRAP_CONFIGURATION)
        _close_startup_channel(channel)
        return 1

    config = handover.config
    handshake = _Handshake(channel, handover.handshake_nonce)
    try:
        set_config(config)
        # Installing the configuration is not enough: ``_make_browser`` reads the
        # browser mode from a module global that defaults to headless, as the
        # frontend's own entry point does too.
        set_headless(config.browser.headless)
        # Before anything can reach an auth gate. `create_owner_server` records it
        # too, but that runs several steps later, inside `_serve`.
        set_process_role(ServerRole.OWNER)
        profile = Path(config.browser.user_data_dir).expanduser().resolve()
        auth_root = auth_root_dir(profile)
    except BaseException:
        bootstrap.report(BOOTSTRAP_STATE)
        handshake.abort()
        handshake.close()
        _close_startup_channel(channel)
        return 1

    lock: DaemonLock | None = None
    try:
        try:
            lock = _take_lock(auth_root)
        except Exception:
            bootstrap.report(BOOTSTRAP_LOCK)
            handshake.abort()
            return 1
        if lock is None:
            handshake.retry()
            return 0

        try:
            log_path = _attach_daemon_log(auth_root)
        except BaseException:
            bootstrap.report(BOOTSTRAP_LOG)
            handshake.abort()
            return 1
        bootstrap.attached(log_path, handover.handshake_nonce)

        try:
            configure_logging(log_level=config.server.log_level, json_format=True)
        except BaseException:
            handshake.fail()
            return 1

        try:
            return asyncio.run(
                _serve(
                    lock=lock,
                    auth_root=auth_root,
                    profile=profile,
                    config=config,
                    log_path=log_path,
                    handshake=handshake,
                    handshake_nonce=handover.handshake_nonce,
                    control=cast(StartupControl, channel),
                    job_name=args.job_name,
                )
            )
        except Exception:
            logger.exception("The daemon stopped with an error")
            handshake.fail()
            return 1
    finally:
        # After the verdict either way, so a frontend blocked on the pipe is
        # released even by a path that forgot to answer. The input is closed
        # only if no read is parked in it, so this cannot wait on a frontend
        # that stays open without committing, and the lock below is always
        # reached.
        bootstrap.close()
        handshake.close()
        _close_startup_channel(channel)
        if lock is not None:
            lock.release()


def _read_handover(
    channel: startup.OwnerChannel | None,
    *,
    timeout: float = _STARTUP_PROBE_SECONDS,
) -> daemon_config.OwnerHandover:
    """Answer HELLO and read CONFIG, without letting a suspended parent pin us.

    One budget for the whole greeting, since to the frontend it is one wait.
    The read runs in a thread because a blocking pipe read has no timeout of its
    own on Windows; a read left parked by the timeout is harmless, because this
    process then exits.
    """
    if channel is None:
        raise OSError("The daemon has no startup channel")
    received: queue.Queue[tuple[str, dict[str, object]] | BaseException] = queue.Queue(
        maxsize=1
    )

    def read() -> None:
        try:
            received.put(channel.accept(build=daemon_version.build_identity()))
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            received.put(exc)

    threading.Thread(target=read, name="daemon-config-read", daemon=True).start()
    try:
        answer = received.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        raise TimeoutError(
            "The daemon starter did not provide its configuration in time"
        ) from None
    if isinstance(answer, BaseException):
        raise answer
    nonce, payload = answer
    return daemon_config.OwnerHandover(daemon_config.decode(payload), nonce)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
