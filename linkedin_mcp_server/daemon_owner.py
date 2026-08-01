"""The process that holds the browser and serves it over loopback.

Started by a frontend that wants a shared browser, and from then on independent
of it: it holds the daemon lock for its lifetime, listens on an ephemeral
loopback port, and drives the one Chromium every client shares.

"Independent" is a POSIX measurement, not a promise on every platform. There the
child leads its own session, and after ``SIGKILL`` to the frontend's whole
process group it was still running and had reparented to pid 1. Windows has no
equivalent in this code: ``start_new_session`` is POSIX-only
(``subprocess.py:795``), so nothing there detaches the child from a job object
that takes its parent down. That is worth revisiting before the flag is turned
on by default, and it is not a regression today, since the feature is opt-in.

Three orderings in here are load-bearing, and each of them is a way the daemon
would otherwise wedge:

*Listen, prove, then publish.* The descriptor is what makes the daemon
discoverable, so writing it before the endpoint answers advertises a server that
refuses tokens. The endpoint is proved by an authenticated request against the
socket, not by the absence of an exception during startup.

*Bind before publishing, and publish the bound port.* The port is chosen by the
kernel, so it does not exist until the socket is bound. Binding here rather than
letting uvicorn do it is what makes the published port the one being served.

*Signal ready last.* The frontend is waiting on a pipe, and everything it does
next assumes the descriptor is readable. Signalling earlier turns a rare startup
loss into an attach against a file that is not there yet.

The lock arrives one of two ways, and the split is a platform fact rather than a
preference. On POSIX the frontend takes the lock first and hands the descriptor
over, so no window exists in which the position looks free. Windows cannot do
that (``daemon_lock.py:54-71``, measured), so the frontend takes no lock at all
and this process competes for it. Losing that race is an ordinary outcome there:
another owner is starting, and the frontend waits for whoever wins.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import hmac
import logging
import os
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TextIO

import httpx

from linkedin_mcp_server import __version__, daemon_config, daemon_descriptor
from linkedin_mcp_server.config import set_config
from linkedin_mcp_server.config.schema import AppConfig
from linkedin_mcp_server.daemon_lock import DaemonLock, DaemonLockError
from linkedin_mcp_server.daemon_liveness import (
    CALL_HEADER,
    HEARTBEAT_PATH,
    call_id_in,
    get_liveness,
)
from linkedin_mcp_server.drivers.browser import set_headless
from linkedin_mcp_server.logging_config import configure_logging
from linkedin_mcp_server.server_role import (
    ServerRole,
    set_process_role,
    stand_down_reason,
)
from linkedin_mcp_server.session_state import auth_root_dir, get_runtime_id

logger = logging.getLogger(__name__)

#: What the frontend reads from the handshake pipe. One line, so a partially
#: written message cannot be mistaken for a complete one.
READY = "ready"
FAILED = "failed"

#: Where the owner serves MCP. Fixed rather than configurable: the frontend
#: reads it out of the descriptor, and the only thing a second value would do is
#: give two installations a way to disagree.
MCP_PATH = "/mcp"

#: How long the owner has to get from a bound socket to a proved endpoint, for
#: both stages together. Generous, because it covers the first import of the
#: whole server graph on a cold page cache.
#:
#: At most half the frontend's own election budget
#: (``daemon_election.DEFAULT_ELECTION_SECONDS``), which is what the test
#: enforces. The frontend stops a child that has said nothing by the time its
#: budget runs out, so an owner allowed to take longer would be killed on a slow
#: machine while still inside its own rules. The margin covers what the frontend
#: spends before this clock even starts: handing the configuration over, and any
#: lock attempts before that.
_STARTUP_PROBE_SECONDS = 30.0

_LOG_FILE = "daemon.log"


def daemon_log_path(auth_root: Path) -> Path:
    """Where a detached owner's output goes.

    A detached process has no terminal, so anything it writes to standard error
    is lost. That includes the traceback of a failure before logging is
    configured, which is exactly the failure a user would need to see. The
    frontend opens this file and hands it to the child as both streams, so even
    an interpreter that dies during imports leaves its reason behind.

    Both sides derive the path from the auth root rather than passing it, so
    they cannot disagree about which file the descriptor names.
    """
    return daemon_descriptor.daemon_dir(auth_root) / _LOG_FILE


def _bind_loopback() -> socket.socket:
    """Bind an ephemeral loopback port, and return the listening socket.

    Bound here rather than by uvicorn because the port has to be known before
    the descriptor is written, and with port 0 the kernel only settles it at
    bind time. Uvicorn accepts an already-bound socket and serves it as is.

    IPv6 first, falling back to IPv4. A host with IPv6 disabled is ordinary
    enough that failing there would be a daemon that cannot start for reasons
    the user did not choose.
    """
    for family, host in ((socket.AF_INET6, "::1"), (socket.AF_INET, "127.0.0.1")):
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            # Deliberately no SO_REUSEADDR. On an ephemeral port there is
            # nothing to reuse, and on the BSDs, macOS among them, it would let
            # a second bind succeed against a live listener on some
            # configurations. A port the kernel just handed out is unused.
            sock.bind((host, 0))
            sock.listen(128)
        except OSError as exc:
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

    The descriptor stores a bare host and brackets it when it builds a URL
    (``daemon_descriptor.py:576-585``), so bracketing here would produce
    ``[[::1]]``.
    """
    address = sock.getsockname()[0]
    return str(address)


def direct_http_client(*, timeout: float) -> httpx.Client:
    """An HTTP client that talks to this machine and nowhere else.

    Every request the daemon makes carries the bearer token for a server driving
    a logged-in LinkedIn session, and every one of them is addressed to
    loopback. httpx honours ``HTTP_PROXY`` by default, and it does so even for
    ``127.0.0.1`` unless ``NO_PROXY`` happens to say otherwise. Reproduced
    against a capture proxy: a request to ``http://127.0.0.1:9/...`` arrived at
    the proxy complete with ``Authorization: Bearer <token>``.

    So proxies are refused rather than avoided by hoping the environment is
    configured well. ``trust_env=False`` also drops ``.netrc`` and environment
    certificate settings, none of which have any business on a loopback hop.

    This is the same distinction the browser configuration already draws: the
    user's proxy is for LinkedIn's traffic, not for the server's own
    (``config/schema.py:105-107``).
    """
    return httpx.Client(trust_env=False, timeout=timeout)


def direct_async_http_client(
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    **extra: Any,
) -> httpx.AsyncClient:
    """The asynchronous counterpart, shaped as FastMCP's client factory.

    The three named parameters are the ``McpHttpClientFactory`` protocol
    (``mcp/shared/_httpx_utils.py``). ``**extra`` is there because the protocol
    is not the whole contract: FastMCP's own transport passes
    ``follow_redirects`` on top of it and marks the call ``type: ignore``
    itself (``fastmcp/client/transports/http.py:176-181``), while the SDK's path
    passes exactly the three. A factory that accepted only the documented
    signature failed at connect time with an unexpected keyword — measured, and
    it took every real-owner test with it.

    ``trust_env`` is the one thing a caller may not override, so it is set after
    the passthrough rather than merged into it.
    """
    extra.pop("trust_env", None)
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout if timeout is not None else httpx.Timeout(30.0),
        auth=auth,
        trust_env=False,
        **extra,
    )


async def _probe(url: str, token: str) -> None:
    """Prove the endpoint answers this token before anything is published.

    An initialize round trip rather than a bare connection. A TCP connect
    succeeds the moment the socket is listening, which it is before uvicorn has
    a single route mounted, and a token that is not accepted would then only
    surface at the first real tool call, in a different process, as a failure
    nobody can place.
    """
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    async with Client(
        StreamableHttpTransport(
            url, auth=token, httpx_client_factory=direct_async_http_client
        )
    ) as client:
        await client.ping()


#: The route a newer frontend uses to ask a stale owner to stand down. Part of
#: the daemon's own protocol, so a change to *this* route is a
#: ``PROTOCOL_VERSION`` bump: every turnover depends on it, and a frontend that
#: guessed wrong about it would wait out its whole budget against a held lock.
#:
#: Adding a route beside it is a different question, answered where the version
#: is defined. The heartbeat route was added without a bump because both sides
#: work without it; this one they do not.
STAND_DOWN_PATH = "/control/stand-down"


def _matches_token(presented: str, expected: str) -> bool:
    """Whether a presented bearer token is the expected one.

    Through digests rather than by comparing the strings directly, and that is
    not belt and braces. ``hmac.compare_digest`` refuses two *strings* when
    either contains a non-ASCII character, and the presented one arrives in an
    HTTP header from anything that can reach the port. Compared directly, a
    header of ``Bearer töken`` raises ``TypeError`` inside the route and the
    caller gets a 500 where it should get a 401 — an unauthenticated request
    turned into a way to provoke an error. Found by trying it.

    Digesting first also makes the comparison length-independent, so nothing
    about the real token's length is observable.
    """
    return hmac.compare_digest(
        hashlib.sha256(presented.strip().encode("utf-8", "surrogatepass")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def create_owner_server(
    *,
    config: AppConfig,
    token: str,
    host: str,
    port: int,
    stand_down: Callable[[], None] | None = None,
) -> Any:
    """Build the authenticated HTTP server the owner runs.

    Uvicorn directly rather than ``FastMCP.run_http_async``, which would
    otherwise be the obvious call. It builds its own server, binds its own port
    and blocks with no handle to stop it (``fastmcp/server/mixins/transport.py``,
    the ``server.serve`` at the end of ``run_http_async``). This process needs
    all three: the port before it can publish, the started flag before it can
    probe, and a handle to stop with — used here by the stand-down turnover, and
    by the idle exit when that is built. Verified against the installed 3.4.4,
    including that a lifespan still runs both ways round when driving uvicorn
    like this.
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

            The token is checked here rather than left to the server's auth
            provider. Measured on 3.4.4: a custom route is mounted outside the
            authentication middleware, so an unauthenticated POST to this path
            was served. Without this check any process on the machine, and any
            page the user's browser visits, could stop the shared browser at
            will.
            """
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not _matches_token(presented, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            stand_down()
            return JSONResponse({"standing_down": True})

    @mcp.custom_route(HEARTBEAT_PATH, methods=["POST"])
    async def heartbeat_route(request: Request) -> JSONResponse:
        """Note that somebody is still waiting for a call this owner is running.

        The same explicit token check as the route above, for the same measured
        reason: a custom route is mounted outside the authentication middleware,
        so without this any process on the machine could keep a call alive that
        its own frontend had already abandoned.

        A call this owner does not know gets ``watched: false`` rather than an
        error. It is the ordinary race, not a fault: a beat sent while the call
        was returning arrives after it was released, and the frontend has
        nothing useful to do about that.
        """
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        if scheme.lower() != "bearer" or not _matches_token(presented, token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        call_id = call_id_in(request.headers.get(CALL_HEADER))
        if call_id is None:
            return JSONResponse({"error": "no call named"}, status_code=400)
        return JSONResponse({"watched": get_liveness().heard(call_id)})

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


async def _serve(
    *,
    lock: DaemonLock,
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    log_path: Path,
    ready: Handshake,
) -> int:
    """Run the endpoint, publish it, and hold it until shutdown."""
    instance_id = daemon_descriptor.new_instance_id()
    token = daemon_descriptor.new_token()

    sock = _bind_loopback()
    host, port = _endpoint_host(sock), sock.getsockname()[1]

    # A list so the route can reach it before the server it belongs to exists.
    turnover: list[str] = []

    def stand_down() -> None:
        turnover.append("asked")

    server = create_owner_server(
        config=config, token=token, host=host, port=port, stand_down=stand_down
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
        deadline = time.monotonic() + _STARTUP_PROBE_SECONDS
        await _await_started(server, serving, timeout=_STARTUP_PROBE_SECONDS)
        # Built before it is proved, and proved through its own ``url``: a probe
        # against a URL assembled here would pass while the one clients actually
        # use was malformed. An IPv6 endpoint is exactly that case, since the
        # literal has to be bracketed.
        await asyncio.wait_for(
            _probe(descriptor.url, token), max(deadline - time.monotonic(), 0.0)
        )

        daemon_descriptor.publish(auth_root, descriptor, token)
        # The idle clock starts here rather than at startup: before the
        # descriptor is published nobody can reach this owner, and counting the
        # import and the browser launch as quiet time would let a short timeout
        # expire before the frontend that asked for it could call.
        get_liveness().the_endpoint_is_live()
        # Only now, and only while the lock is held: a token file that is not
        # this generation's belongs to an owner that is gone, because a live one
        # would be holding the lock this process has.
        _forget_superseded_tokens(auth_root, keeping=instance_id)
        logger.info("Daemon listening on %s", descriptor.url)
        ready.succeed()
    except BaseException:
        server.should_exit = True
        # Through the same bounded stop as the stand-down path, and for the same
        # reason: `wait_for` waits for the cancellation it requested to finish,
        # so a task that suppresses cancellation makes this line unbounded no
        # matter what timeout it is given. `suppress` only helps once the await
        # returns. This process holds the daemon lock by now, so a failed
        # startup that never exits is a profile nothing can elect an owner for.
        await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS)
        raise

    del lock  # held for the process lifetime; named to say so
    await _serve_until_stopped(
        server, serving, turnover, config.browser.browser_idle_timeout_seconds
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
#: preserved: no client was ever told this owner existed, and the descriptor was
#: never published.
_FAILED_STARTUP_SHUTDOWN_SECONDS = 10.0


async def _serve_until_stopped(
    server: Any,
    serving: asyncio.Task[None],
    turnover: list[str],
    idle_timeout: float = 0.0,
) -> None:
    """Hold the endpoint open until it stops, or until asked to give way.

    Two things ask, for unrelated reasons, and they are noticed in the same place
    because the answer is the same: a newer build wants the browser, or this
    process can no longer drive it. Both are noticed here rather than acted on
    where they arise, so the request in flight gets its reply before this process
    goes away. Stopping mid-request would leave the caller unable to tell a
    completed handover from a refusal, and it would then wait for a lock this
    process had not yet freed.
    """
    while not serving.done():
        wedged = stand_down_reason()
        if wedged is not None:
            # Not recoverable in place, which is what separates this from every
            # other error an owner reports and keeps serving after. The profile
            # is held until this process exits, so exiting *is* the recovery: the
            # kernel frees the daemon lock and the next call elects an owner that
            # can open the profile. Nothing is lost, because the session lives on
            # disk rather than in this process.
            logger.warning("Standing down: %s", wedged)
            server.should_exit = True
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS)
            return
        if turnover:
            logger.info("A newer build asked for the browser; standing down")
            server.should_exit = True
            # Graceful, but not unconditionally. Uvicorn finishes the requests
            # in flight and runs the lifespan, which closes the browser, and
            # only then does this process exit and the kernel free the lock.
            #
            # Bounded, because that shutdown is not guaranteed to finish:
            # ``timeout_graceful_shutdown`` bounds the connection tasks and
            # nothing bounds the lifespan behind them. An owner stuck there
            # would hold the daemon lock forever, having already promised a
            # frontend that it was standing down — every later election would
            # find the position occupied by a process that is no longer serving.
            # Giving up the wait and exiting is the lesser harm: the lock is
            # freed by the exit either way.
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS)
            return
        # Cheap enough to do on the same tick as the two questions above: one
        # dictionary scan over the calls in flight, on a process that is already
        # polling. A timer of its own would be a second thing to shut down.
        liveness = get_liveness()
        liveness.cancel_the_abandoned()
        # And the third reason to go, after wedge and turnover. An owner holds
        # the daemon lock for the machine's uptime otherwise, having closed the
        # browser hours ago: the process is what the next election has to wait
        # for, not the Chromium it is no longer running.
        #
        # `quiet_for` answers None while any call is in flight, marked or not,
        # so this cannot cut one off. The stale descriptor is left behind
        # deliberately: the next election probes it, is refused, and elects a
        # replacement, whereas deleting it here would race whoever publishes
        # next.
        quiet = liveness.quiet_for()
        if idle_timeout > 0 and quiet is not None and quiet >= idle_timeout:
            logger.info("Nothing has needed the browser in %.0fs; exiting", quiet)
            server.should_exit = True
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS)
            return
        await asyncio.sleep(_STAND_DOWN_POLL_SECONDS)
    await serving


async def _stop_within(serving: asyncio.Task[None], seconds: float) -> None:
    """Let the server finish shutting down, and stop the process if it will not.

    Ending the *wait* is not the same as ending the process, and the difference
    is the whole point here. Returning from this leaves the serving task pending;
    ``asyncio.run`` then cancels it on the way out and waits, without any bound,
    for that cancellation to complete. A task that suppresses cancellation
    therefore keeps the interpreter alive, holding the daemon lock, after every
    timeout above it has already fired.

    That is not a hypothetical shape. ``close_browser`` deliberately holds
    cancellation back until teardown finishes (``drivers/browser.py:736-757``),
    because interrupting it half way leaves a Chromium nobody owns, and the
    export it waits on first is itself unbounded. An unresponsive browser is
    exactly the case where a stand-down must still end.

    So the last resort is a hard exit. It skips interpreter cleanup, which here
    means skipping the very teardown that is already stuck; the kernel closes
    the descriptors and frees the lock, which is what the next election needs.
    Measured before this existed: the helper returned on time and the process
    never came out of ``asyncio.run``.
    """
    try:
        await asyncio.wait_for(asyncio.shield(serving), seconds)
        return
    except TimeoutError:
        logger.error(
            "The daemon did not finish shutting down in %.0fs; exiting hard so "
            "the lock is released for the next owner",
            seconds,
        )
    except Exception:
        logger.warning("The daemon endpoint stopped with an error", exc_info=True)
        return

    _exit_hard()


def _exit_hard() -> int:  # pragma: no cover - the process does not come back
    """Leave immediately, without running interpreter cleanup.

    Separated so a test can substitute it. Nothing after this returns.
    """
    logging.shutdown()  # flush the log file; the traceback above is the record
    os._exit(1)


async def _await_started(
    server: object,
    serving: asyncio.Task[None],
    *,
    timeout: float = _STARTUP_PROBE_SECONDS,
) -> None:
    """Wait until uvicorn reports it is serving, or until it gives up.

    Polled rather than awaited on an event, because uvicorn exposes only the
    flag. Watching the serving task alongside it is what turns a server that
    died during startup into an error instead of a wait that never ends.

    Bounded as well, because "died" is not the only way startup fails. An ASGI
    lifespan that hangs leaves the task pending and the flag false forever, and
    by this point the child already holds the daemon lock: the frontend would
    time out and move on while this process kept the position occupied without
    ever serving anything. Raising here runs the failure path, which stops the
    server and lets the process exit, and the kernel frees the lock with it.
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


def _forget_superseded_tokens(auth_root: Path, *, keeping: str) -> None:
    """Remove token files from earlier generations of this daemon.

    ``publish`` writes one per instance and removes none
    (``daemon_descriptor.py:754-790``), so without this every restart leaves
    another credential file behind. Safe only here: the caller holds the lock,
    so no other owner exists, and the descriptor for *keeping* is already
    published, so no client is about to read one of these.
    """
    directory = daemon_descriptor.daemon_dir(auth_root)
    keep = daemon_descriptor.token_path(auth_root, keeping).name
    try:
        entries = list(directory.iterdir())
    except OSError:
        logger.debug("Could not list the daemon directory", exc_info=True)
        return
    for entry in entries:
        if entry.name.startswith("token-") and entry.name != keep:
            with contextlib.suppress(OSError):
                entry.unlink()


class Handshake(Protocol):
    """How this process tells the frontend how startup went.

    Stated as a protocol because the ordering it participates in is the thing
    worth testing — ready must come after the descriptor is on disk — and a test
    that can observe when it is called does not need a pipe to do it.
    """

    def succeed(self) -> None: ...

    def fail(self) -> None: ...

    def close(self) -> None: ...


class _Handshake:
    """The pipe the frontend waits on, and the one message that goes through it.

    Standard output, taken over at startup and closed after the verdict. Not an
    inherited descriptor of its own: ``pass_fds`` is POSIX-only
    (``subprocess.py:1464``), and the platform without it is precisely the one
    where the frontend has nothing else to go on, because it cannot hand over a
    lock either and so cannot know whether this child won.

    A pipe rather than a file, because the interesting failure is silence. When
    this process dies the frontend's read ends at once, so an owner that never
    reaches a verdict is reported as a failed election rather than waited out.
    """

    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream

    def succeed(self) -> None:
        self._write(READY)

    def fail(self) -> None:
        self._write(FAILED)

    def _write(self, message: str) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.write(f"{message}\n")
            stream.flush()
        except (OSError, ValueError):
            logger.debug("The startup handshake could not be written", exc_info=True)
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    def close(self) -> None:
        """Close without a verdict, so the frontend sees the pipe end."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _claim_handshake_stream() -> TextIO | None:
    """Take standard output for the handshake, and point everything else away.

    Two things at once, and both matter. The frontend reads this pipe for one
    line, so anything else written to it would be read as a verdict. And the
    pipe stops being read the moment the frontend has its answer, so a process
    that kept writing there would eventually block on a full buffer or die of a
    broken pipe — for a daemon that outlives its starter by design, that is a
    hang with no visible cause.

    So the descriptor is duplicated for this module's use and the original is
    replaced by the log file, which the frontend opened as standard error. Every
    later ``print``, every library that greets the terminal, and every logging
    handler built afterwards goes to the log instead.
    """
    try:
        sys.stdout.flush()
        duplicate = os.dup(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        # No usable standard output, which means nobody is waiting on a
        # handshake either: this was started by hand rather than by a frontend.
        # Running on without one is the right outcome, since the endpoint and
        # the descriptor do not depend on it.
        logger.debug("No handshake stream to claim", exc_info=True)
        return None
    try:
        # Onto the log the frontend already gave us, so nothing is lost and
        # nothing else reaches the handshake pipe.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        os.close(duplicate)
        logger.debug("Standard output could not be redirected", exc_info=True)
        return None
    return os.fdopen(duplicate, "w", encoding="utf-8")


def _take_lock(auth_root: Path, inherited_fd: int | None) -> DaemonLock | None:
    """Hold the daemon lock, by adoption on POSIX or by contest on Windows."""
    lock = DaemonLock(auth_root)
    if inherited_fd is not None:
        lock.adopt(inherited_fd)
        return lock
    # Windows, where the frontend cannot hand a lock over. Losing here is not a
    # defect: another owner is coming up, and the frontend waits for it.
    if not lock.try_acquire():
        return None
    return lock


def main(argv: list[str] | None = None) -> int:
    """Run as the detached owner. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="linkedin-mcp-daemon",
        description="Serve one shared LinkedIn browser to local MCP clients.",
    )
    parser.add_argument("--lock-fd", type=int, default=None)
    args = parser.parse_args(argv)

    # Before anything else can write to it.
    handshake = _Handshake(_claim_handshake_stream())
    try:
        # Read before anything else touches the configuration: the frontend
        # holds the pipe open only until it has written, and the settings decide
        # how this process logs.
        config = _read_config()
    except BaseException:
        handshake.fail()
        raise

    set_config(config)
    # Installing the configuration is not enough: the browser mode lives in a
    # module global that defaults to headless (``drivers/browser.py:63``), and
    # ``_make_browser`` reads that global rather than the configuration
    # (``drivers/browser.py:265-283``). The frontend's own entry point sets it
    # explicitly for the same reason (``cli_main.py:391``).
    #
    # Without this, an owner started with ``--no-headless`` publishes a
    # fingerprint that says so — the frontend compares it and attaches happily —
    # and then opens headless anyway. A user who asked to watch the browser
    # would get no window and no error.
    set_headless(config.browser.headless)
    # Before anything can reach an auth gate. `create_owner_server` records it
    # too, but that runs several steps later, inside `_serve`: a failure between
    # here and there would be handled by a process that still believed it had a
    # terminal. This process has neither one nor a desktop session for its whole
    # life, so it says so as early as it says anything.
    set_process_role(ServerRole.OWNER)
    configure_logging(log_level=config.server.log_level, json_format=True)

    profile = Path(config.browser.user_data_dir).expanduser().resolve()
    auth_root = auth_root_dir(profile)

    lock: DaemonLock | None = None
    try:
        lock = _take_lock(auth_root, args.lock_fd)
        if lock is None:
            logger.info("Another process won the daemon election")
            handshake.fail()
            return 0
        return asyncio.run(
            _serve(
                lock=lock,
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=daemon_log_path(auth_root),
                ready=handshake,
            )
        )
    except DaemonLockError:
        logger.exception("The daemon could not take ownership")
        handshake.fail()
        return 1
    except Exception:
        logger.exception("The daemon stopped with an error")
        handshake.fail()
        return 1
    finally:
        # After the verdict either way, so a frontend blocked on the pipe is
        # released even by a path that forgot to answer.
        handshake.close()
        if lock is not None:
            lock.release()


def _read_config() -> AppConfig:
    """Read the handed-over configuration from standard input.

    Read to end of file rather than one line, and the frontend closes its end
    once written. That is the whole framing: a short read cannot be mistaken for
    a complete message because a truncated JSON object does not parse.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("The daemon was started without a configuration")
    return daemon_config.decode(raw)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
