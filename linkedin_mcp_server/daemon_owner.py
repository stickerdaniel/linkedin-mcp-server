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

The startup boundary is load-bearing. This child listens, proves its own
authenticated endpoint, and writes only a generation-specific pending descriptor.
The frontend validates that state and sends one commit record. The lock-holding
child then performs the same-directory replacement: EOF before the record aborts,
while parent loss after it cannot revoke an owner that may already be canonical.

The port is chosen by the kernel, so the socket is bound before the descriptor is
built. The endpoint is proved through the descriptor's own URL and token before
the child reports `prepared`; no globally discoverable state exists before that
proof. Publication belongs to the lock holder, so a stale Windows frontend can
never outlive its child and overwrite the next winner.

The child opens its own log and competes for the lock on every platform. Keeping
those potentially blocking state-storage operations behind the process boundary
lets the frontend enforce its deadline by killing this process. A timed-out
thread could later acquire the lock or publish over an in-process fallback; a
process with a pending hard kill cannot return to user space and do either.
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
import queue
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, NoReturn, Protocol, TextIO

import httpx

from linkedin_mcp_server import (
    __version__,
    daemon_config,
    daemon_descriptor,
    process_control,
)
from linkedin_mcp_server.bootstrap import (
    browser_setup_failure_pending,
    browser_setup_in_progress,
)
from linkedin_mcp_server.common_utils import is_still_at
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
from linkedin_mcp_server.private_state import (
    PrivateStateError,
    harden_created_file,
    harden_file,
)
from linkedin_mcp_server.process_tree import WindowsJob, hard_exit_process_tree
from linkedin_mcp_server.profile_lease import _release_locked_fd
from linkedin_mcp_server.server_role import (
    ServerRole,
    hard_exit_required,
    set_process_role,
    stand_down_reason,
)
from linkedin_mcp_server.session_state import auth_root_dir, get_runtime_id

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"

#: Records carried by the authenticated startup control and handshake pipes. The
#: nonce is handed over only after this process exists, so interpreter startup
#: output cannot forge them.
HANDSHAKE = "owner"
READY = "ready"
COMMIT = "commit"
PREPARED = "prepared"
COMMITTED = "committed"
FAILED = "failed"
ABORTED = "aborted"
RETRY = "retry"
UNCERTAIN = "uncertain"

#: Fixed bootstrap records carried on standard error before the daemon log is
#: available. They contain no configuration values or exception text.
BOOTSTRAP_PREFIX = "daemon-bootstrap:"
BOOTSTRAP_CONFIGURATION = "configuration"
BOOTSTRAP_STATE = "state"
BOOTSTRAP_LOG = "log"
BOOTSTRAP_ATTACHED = "attached"

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
_UNCERTAIN_PUBLICATION_RETRY_SECONDS = 0.2
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
        try:
            staged.rename(log_path)
        except FileExistsError:
            harden_file(log_path)
            return
        published = True
        harden_file(log_path)
    finally:
        # A published log is left where it is, even when the verification after
        # it failed. Another candidate may already have opened this inode and be
        # writing its own startup into it, and this process cannot tell; an
        # empty file hardened before publication is the cheaper thing to leave.
        if not published:
            with contextlib.suppress(OSError):
                staged.unlink()


def _attach_daemon_log(auth_root: Path) -> Path:
    """Open the daemon log inside the child and attach both output streams.

    This may block on unavailable state storage. It therefore runs only in the
    owner process, which the frontend can hard-kill without leaving a worker that
    might later acquire the lock or publish.
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
        try:
            descriptor = os.open(log_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # A second candidate reached the same absent path. Only the process
            # that created the file may remove it again below, or a failure here
            # unlinks the log the candidate that won the lock is writing to.
            created = False
            harden_file(log_path)
            descriptor = os.open(log_path, flags, 0o600)
    else:
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
        # gives: the loser of the creation race above is already appending to
        # this inode, and an owner-only empty file costs nothing to leave.
        os.close(descriptor)
    return log_path


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
    auth_root: Path,
    instance_id: str,
    deadline: float,
    *,
    maintenance: Callable[[], None] | None = None,
) -> None:
    """Commit after transient Windows descriptor readers release their handles."""
    while True:
        if maintenance is not None:
            maintenance()
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


def _start_canonical_read(
    auth_root: Path,
) -> asyncio.Future[daemon_descriptor.DaemonDescriptor | None]:
    """Start one canonical read whose native thread is never cancelled."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[daemon_descriptor.DaemonDescriptor | None] = (
        loop.create_future()
    )

    def read() -> None:
        try:
            descriptor = daemon_descriptor.read(auth_root)
        except BaseException as exc:  # noqa: BLE001 - delivered to the event loop

            def fail(error: BaseException = exc) -> None:
                if not result.done():
                    result.set_exception(error)

            complete = fail
        else:

            def succeed(
                value: daemon_descriptor.DaemonDescriptor | None = descriptor,
            ) -> None:
                if not result.done():
                    result.set_result(value)

            complete = succeed
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete)

    threading.Thread(target=read, name="daemon-canonical-read", daemon=True).start()
    return result


def _exit_uncertain_publication(_reason: str, *, lock: DaemonLock | None) -> NoReturn:
    """Exit without I/O or unwinding after an already logged ambiguity.

    *lock* is handed down rather than looked up, for the reason it is handed
    down through :func:`_stop_within`: this exit drains the process tree with no
    bound, and an election left held for that drain is a profile no successor
    can take over.
    """
    _exit_hard(lock)
    raise RuntimeError("The hard exit returned during uncertain publication")


def _require_uncertain_endpoint(
    server: Any, serving: asyncio.Task[None], *, lock: DaemonLock | None
) -> None:
    """Refuse to publish an uncertain generation after its endpoint stops."""
    if getattr(server, "should_exit", False):
        _exit_uncertain_publication(
            "The daemon endpoint was asked to stop during publication reconciliation",
            lock=lock,
        )
    if not serving.done():
        return
    if not serving.cancelled():
        with contextlib.suppress(BaseException):
            serving.exception()
    _exit_uncertain_publication(
        "The daemon endpoint stopped during publication reconciliation", lock=lock
    )


def _maintain_uncertain_publication(
    server: Any,
    serving: asyncio.Task[None],
    turnover: list[str],
    *,
    lock: DaemonLock | None,
) -> None:
    """Keep frontend-visible lifecycle controls active before startup completes."""
    _require_uncertain_endpoint(server, serving, lock=lock)
    if stand_down_reason() is not None:
        server.should_exit = True
        _exit_uncertain_publication(
            "The daemon became unable to serve during publication reconciliation",
            lock=lock,
        )
    if turnover:
        server.should_exit = True
        _exit_uncertain_publication(
            "A newer build requested stand-down during publication reconciliation",
            lock=lock,
        )
    get_liveness().cancel_the_abandoned()


async def _await_uncertain_read_until(
    read: asyncio.Future[daemon_descriptor.DaemonDescriptor | None],
    deadline: float,
    maintenance: Callable[[], None],
) -> daemon_descriptor.DaemonDescriptor | None:
    """Wait for canonical state while preserving the owner's maintenance cadence."""
    while True:
        maintenance()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            return await asyncio.wait_for(
                asyncio.shield(read), min(_STAND_DOWN_POLL_SECONDS, remaining)
            )
        except TimeoutError:
            if read.done():
                return read.result()
            if time.monotonic() >= deadline:
                raise


async def _sleep_during_uncertain_publication(
    seconds: float, maintenance: Callable[[], None]
) -> None:
    """Sleep without leaving lifecycle maintenance dormant between retries."""
    deadline = time.monotonic() + seconds
    while True:
        maintenance()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_STAND_DOWN_POLL_SECONDS, remaining))


async def _reconcile_uncertain_publication(
    auth_root: Path,
    instance_id: str,
    *,
    server: Any,
    serving: asyncio.Task[None],
    lock: DaemonLock | None,
    turnover: list[str] | None = None,
) -> None:
    """Keep a live endpoint locked until its publication is provable.

    An error from ``os.replace`` can describe either side of the rename on NFS.
    Releasing the lock on that evidence lets a successor start before a delayed
    canonical entry names this endpoint. The lock holder therefore retries the
    same pending generation until a read observes it or a replacement succeeds.

    Every mutating retry stays synchronously in this task. Once one returns, its
    storage operation is over; only cached visibility may lag. That ordering lets a
    later lock holder safely replace this generation after a hard process exit. A
    worker left running outside this task would break the ordering by publishing
    after the lock had moved to a successor. Canonical reads are non-mutating, but
    an unavailable mount can block their native threads, so reconciliation reuses
    one in-flight read until it settles instead of creating one per retry. Short
    waits between checks keep call cancellation and stand-down handling alive for
    a descriptor that became visible before publication could be proved.

    There is deliberately no elapsed-time fallback. A persistent error that is not
    structurally definitive carries no proof that publication failed, so any timed
    release can admit a stale generation after its successor. The owner remains
    undiscoverable and holds the lock until storage recovers. Moving coordination
    state off remote storage, tracked by #796, is what can add a bounded terminal
    path without weakening this invariant.

    Every terminal path out of this loop is a hard exit, so *lock* comes in with
    the call: the exit releases the election before its unbounded process-tree
    drain, and the profile lease alone keeps a successor off the browser.
    """
    canonical_read: asyncio.Future[daemon_descriptor.DaemonDescriptor | None] | None = (
        None
    )
    requests = [] if turnover is None else turnover

    def maintenance() -> None:
        _maintain_uncertain_publication(server, serving, requests, lock=lock)

    while True:
        maintenance()
        deadline = time.monotonic() + _COMMIT_AUTH_SECONDS
        canonical: daemon_descriptor.DaemonDescriptor | None = None
        if canonical_read is None:
            try:
                canonical_read = _start_canonical_read(auth_root)
            except Exception:
                # Inside the loop, because a read that will not start is one more
                # thing that has not proved publication, and no such thing may
                # leave here: an escape from this line stops an endpoint the
                # canonical descriptor may already name and releases its lock.
                logger.warning(
                    "Could not start a canonical descriptor read; retrying",
                    exc_info=True,
                )
        if canonical_read is not None:
            try:
                canonical = await _await_uncertain_read_until(
                    canonical_read, deadline, maintenance
                )
            except asyncio.CancelledError:
                _exit_uncertain_publication(
                    "Publication reconciliation was cancelled", lock=lock
                )
            except Exception:
                # Retain a read that timed out: its native thread cannot be
                # cancelled, and starting another on the next pass would leak one
                # thread per retry.
                if canonical_read.done():
                    with contextlib.suppress(BaseException):
                        canonical_read.exception()
                    canonical_read = None
                canonical = None
            except BaseException:
                _exit_uncertain_publication(
                    "Publication reconciliation was interrupted", lock=lock
                )
            else:
                canonical_read = None
        maintenance()
        if canonical is not None and canonical.instance_id == instance_id:
            return

        try:
            await _commit_prepared_until(
                auth_root, instance_id, deadline, maintenance=maintenance
            )
        except asyncio.CancelledError:
            _exit_uncertain_publication(
                "Publication reconciliation was cancelled", lock=lock
            )
        except Exception:
            # No error here authorizes releasing the lock; the next pass asks both
            # the canonical state and the same pending replacement again.
            pass
        except BaseException:
            _exit_uncertain_publication(
                "Publication reconciliation was interrupted", lock=lock
            )
        else:
            try:
                # Commit is synchronous. Let the endpoint task and pending
                # cancellation run before declaring the returned replacement live.
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                _exit_uncertain_publication(
                    "Publication reconciliation was cancelled", lock=lock
                )
            except BaseException:
                _exit_uncertain_publication(
                    "Publication reconciliation was interrupted", lock=lock
                )
            maintenance()
            return

        try:
            await _sleep_during_uncertain_publication(
                _UNCERTAIN_PUBLICATION_RETRY_SECONDS, maintenance
            )
        except asyncio.CancelledError:
            _exit_uncertain_publication(
                "Publication reconciliation was cancelled", lock=lock
            )
        except BaseException:
            _exit_uncertain_publication(
                "Publication reconciliation was interrupted", lock=lock
            )


async def _read_control_until(
    control: process_control.ControlChannel, deadline: float
) -> str:
    """Read one control record without making event-loop shutdown wait on it."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[str] = loop.create_future()

    def read() -> None:
        try:
            decision = control.readline()
        except BaseException as exc:  # noqa: BLE001 - delivered to the event loop

            def fail(error: BaseException = exc) -> None:
                if not result.done():
                    result.set_exception(error)

            complete = fail
        else:

            def succeed(value: str = decision) -> None:
                if not result.done():
                    result.set_result(value)

            complete = succeed
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(complete)

    threading.Thread(target=read, name="daemon-control", daemon=True).start()
    return await asyncio.wait_for(result, max(deadline - time.monotonic(), 0.0))


async def _serve(
    *,
    lock: DaemonLock,
    auth_root: Path,
    profile: Path,
    config: AppConfig,
    log_path: Path,
    handshake: Handshake,
    handshake_nonce: str,
    control: process_control.ControlChannel,
    startup_protocol: int = daemon_config.STARTUP_PROTOCOL_VERSION,
    job_name: str | None = None,
) -> int:
    """Run the endpoint, prepare it, and serve only after parent commit."""
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
        startup_deadline = time.monotonic() + _STARTUP_PROBE_SECONDS
        await _await_started(server, serving, timeout=_STARTUP_PROBE_SECONDS)
        # Built before it is proved, and proved through its own ``url``: a probe
        # against a URL assembled here would pass while the one clients actually
        # use was malformed. An IPv6 endpoint is exactly that case, since the
        # literal has to be bracketed.
        await asyncio.wait_for(
            _probe(descriptor.url, token),
            max(startup_deadline - time.monotonic(), 0.0),
        )

        commit_deadline = time.monotonic() + _COMMIT_AUTH_SECONDS
        predecessor_protocol = not daemon_config.authorizes_commit(startup_protocol)
        _forget_superseded_tokens(auth_root)
        daemon_descriptor.prepare(auth_root, descriptor, token)
        if predecessor_protocol:
            # A predecessor parent has no commit record. Its only safe handoff is
            # READY before publication: after this authenticated verdict it no
            # longer kills a silent child, while the daemon lock prevents another
            # owner from publishing during the remaining commit.
            handshake.ready()
        else:
            handshake.prepared(instance_id)

        # Failed starts deliberately leave this generation's unique pending files.
        # Removing them re-enters account-home storage and can pin this process in
        # the kernel while it still owns the lock. The next successful lock holder's
        # pre-publication sweep removes every superseded generation instead.

        # A predecessor frontend predates parent authorization. READY above ends
        # its termination authority before this child attempts publication; current
        # frontends use the atomic prepare/commit boundary below.
        if not predecessor_protocol:
            try:
                decision = await _read_control_until(control, commit_deadline)
            except TimeoutError:
                logger.warning("The daemon starter did not authorize commit in time")
                handshake.retry()
                server.should_exit = True
                await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
                return 1
            if decision != f"{HANDSHAKE} {handshake_nonce} {COMMIT}\n":
                logger.info("The daemon starter exited before committing this owner")
                handshake.abort()
                server.should_exit = True
                await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
                return 0

        # The parent retains termination authority until this exact commit record.
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
            # terminal without another state read. An error from the replacement
            # itself may instead arrive after an NFS server applied the rename; only
            # that case is reconciled against canonical state.
            if _commit_definitely_failed(exc):
                logger.exception("Daemon descriptor publication failed definitively")
                handshake.abort()
                server.should_exit = True
                await _stop_within(serving, _FAILED_STARTUP_SHUTDOWN_SECONDS, lock=lock)
                raise
            # The replacement may already name this live endpoint. Preserve both
            # its token and its process while the existing reconciliation loop
            # performs the first bounded read. Its maintenance cadence keeps call
            # cancellation, turnover, wedge detection and endpoint death active
            # from that first ambiguous-state wait onward.
            logger.warning(
                "Daemon descriptor publication is ambiguous; retaining the lock "
                "while it is reconciled",
                exc_info=True,
            )
            await _reconcile_uncertain_publication(
                auth_root,
                instance_id,
                server=server,
                serving=serving,
                lock=lock,
                turnover=turnover,
            )

        # Publication is now proved. Only in-memory startup state and the verdict
        # follow, so a blocked log or state mount cannot strand a canonical owner
        # before the frontend learns it is ready.
        get_liveness().the_endpoint_is_live()
        if not predecessor_protocol:
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
        server,
        serving,
        turnover,
        config.browser.browser_idle_timeout_seconds,
        lock=lock,
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

#: How long an owner holds an unconsumed setup failure open for the retry the
#: in-progress message asks for "in a minute or two". Bounded to match that
#: guidance: an abandoned failure must not pin an owner forever.
_SETUP_FAILURE_RETRY_GRACE_SECONDS = 120.0


async def _serve_until_stopped(
    server: Any,
    serving: asyncio.Task[None],
    turnover: list[str],
    idle_timeout: float = 0.0,
    *,
    lock: DaemonLock | None,
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
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS, lock=lock)
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
            # Giving up the wait and exiting is the lesser harm, and that exit
            # frees the election on the way in rather than after its unbounded
            # browser drain (`_exit_hard`).
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS, lock=lock)
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
        # First use starts setup in a detached task and then returns its
        # in-progress response. That task is work for this owner even though no
        # tool call remains in liveness; exiting here would cancel every install
        # that lasts longer than the idle timeout and start it over next time.
        #
        # A failed setup is the other half: only the next tool call consumes the
        # failure, so a shorter idle timeout would send that retry to a fresh
        # owner that starts setup over and hides the diagnostic again.
        quiet_required = idle_timeout
        if browser_setup_failure_pending():
            quiet_required = max(idle_timeout, _SETUP_FAILURE_RETRY_GRACE_SECONDS)
        if (
            idle_timeout > 0
            and quiet is not None
            and quiet >= quiet_required
            and not browser_setup_in_progress()
        ):
            logger.info("Nothing has needed the browser in %.0fs; exiting", quiet)
            server.should_exit = True
            await _stop_within(serving, _STAND_DOWN_SHUTDOWN_SECONDS, lock=lock)
            return
        await asyncio.sleep(_STAND_DOWN_POLL_SECONDS)
    try:
        await serving
    finally:
        if hard_exit_required():
            logger.error(
                "Browser shutdown was not confirmed; exiting hard so the "
                "profile stays held until the browser is gone"
            )
            _exit_hard(lock)


async def _stop_within(
    serving: asyncio.Task[None], seconds: float, *, lock: DaemonLock | None
) -> None:
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
    means skipping the very teardown that is already stuck. Letting the kernel
    free the descriptors is not enough on its own, because that exit drains the
    browser first with no bound; *lock* is handed down so :func:`_exit_hard` can
    free the election before the drain. Measured before this existed: the helper
    returned on time and the process never came out of ``asyncio.run``.
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


def _exit_hard(lock: DaemonLock | None) -> NoReturn:
    """Leave immediately, containing descendants without interpreter cleanup.

    The election goes first and the profile does not. The drain below is
    unbounded on purpose: it holds the profile until the browser groups are
    provably gone, because releasing it earlier hands a live Chromium to
    whoever opens that profile next. The daemon lock says only that an owner
    exists, and this process has stopped being one, so spending that wait on
    the lock too would block every election behind a drain none of them care
    about. The replacement is elected at once and waits on the profile lease
    instead: on this process, whose lease descriptor lives until ``os._exit``,
    and on POSIX also on the crash guardian holding a copy of it.

    Idempotent, so ``main``'s ``finally`` may still release defensively.
    """
    if lock is not None:
        lock.release()
    hard_exit_process_tree(1)


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


def _forget_superseded_tokens(auth_root: Path) -> None:
    """Bound old credentials before preparing the next generation.

    The caller holds the lock, so the canonical descriptor can only name the
    predecessor this process is about to replace. Its token remains until the next
    owner startup. All of this runs before ``prepared`` and publication, while the
    frontend can still kill a child blocked on state storage.
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
    """One fixed diagnostic record before the daemon log can be used."""

    def __init__(self, stream: TextIO | None) -> None:
        self._stream = stream

    def report(self, code: str) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        try:
            stream.write(f"{BOOTSTRAP_PREFIX} {code}\n")
            stream.flush()
        except (OSError, ValueError):
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

    def ready(self) -> None: ...

    def committed(self) -> None: ...

    def fail(self) -> None: ...

    def abort(self) -> None: ...

    def retry(self) -> None: ...

    def uncertain(self) -> None: ...

    def close(self) -> None: ...


class _Handshake:
    """The pipe the frontend waits on through prepare and commit.

    Standard output, taken over at startup and closed after the verdict. Not an
    inherited descriptor of its own: ``pass_fds`` is POSIX-only
    (``subprocess.py:1464``), and the platform without it is precisely the one
    where the frontend has nothing else to go on, because it cannot hand over a
    lock either and so cannot know whether this child won.

    A pipe rather than a file, because the interesting failure is silence. When
    this process dies the frontend's read ends at once, so an owner that never
    reaches a verdict is reported as a failed election rather than waited out.
    """

    def __init__(self, stream: TextIO | None, handshake_nonce: str) -> None:
        self._stream = stream
        self._nonce = handshake_nonce

    def prepared(self, instance_id: str) -> None:
        self._write(f"{PREPARED} {instance_id}", close=False)

    def ready(self) -> None:
        self._write(READY)

    def committed(self) -> None:
        self._write(COMMITTED)

    def fail(self) -> None:
        self._write(FAILED)

    def abort(self) -> None:
        self._write(ABORTED)

    def retry(self) -> None:
        self._write(RETRY)

    def uncertain(self) -> None:
        self._write(UNCERTAIN)

    def _write(self, message: str, *, close: bool = True) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            stream.write(f"{HANDSHAKE} {self._nonce} {message}\n")
            stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            if close:
                self.close()

    def close(self) -> None:
        """Close without a verdict, so the frontend sees the pipe end."""
        stream, self._stream = self._stream, None
        if stream is None:
            return
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _claim_bootstrap_stream() -> TextIO | None:
    """Keep a private copy of standard error until the daemon log is attached."""
    try:
        duplicate = os.dup(sys.stderr.fileno())
    except (OSError, ValueError, AttributeError):
        return None
    return os.fdopen(duplicate, "w", encoding="utf-8")


def _claim_handshake_stream() -> TextIO | None:
    """Take standard output for the handshake, and point everything else away.

    Two things at once, and both matter. The frontend reads only startup records
    from this pipe, so anything else written to it would be read as a verdict. The
    pipe stops being read the moment the frontend has its answer, so a process
    that kept writing there would eventually block on a full buffer or die of a
    broken pipe — for a daemon that outlives its starter by design, that is a
    hang with no visible cause.

    So the descriptor is duplicated for this module's use and the original is
    pointed at standard error. The frontend reads that separate bootstrap pipe for
    one fixed diagnosis until the child opens the daemon log and attaches both
    output streams before logging is configured.
    """
    try:
        sys.stdout.flush()
        duplicate = os.dup(sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        # No usable standard output means the required parent handshake is
        # unavailable. Startup will fail before serving because only the parent
        # can validate and authorize this owner's prepared descriptor.
        return None
    try:
        # Keep ordinary output off the handshake pipe until the child attaches
        # both streams to its own log.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        os.close(duplicate)
        return None
    # newline="\n" is load-bearing rather than tidy. The frontend authenticates
    # this frame by comparing exact bytes, and the default translates the line
    # ending to os.linesep, so on Windows a genuine READY arrives as CRLF and is
    # refused. The frontend then waits out its startup deadline and terminates
    # the Job around an owner that had already published itself.
    return os.fdopen(duplicate, "w", encoding="utf-8", newline="\n")


def _close_handshake_stream(stream: TextIO | None) -> None:
    """End an unarmed handshake without making any startup claim."""
    if stream is not None:
        with contextlib.suppress(OSError, ValueError):
            stream.close()


def _take_lock(auth_root: Path, inherited_fd: int | None) -> DaemonLock | None:
    """Hold the daemon lock, normally by contesting inside this process."""
    lock = DaemonLock(auth_root)
    if inherited_fd is not None:
        lock.adopt(inherited_fd)
        return lock
    # Losing is an ordinary race: another child is coming up, and the frontend
    # waits for whichever one publishes.
    if not lock.try_acquire():
        return None
    return lock


def _abandon_inherited_lock(fd: int) -> None:
    """Unlock a failed handoff even while the suspended parent keeps a copy."""
    with contextlib.suppress(OSError, ValueError):
        _release_locked_fd(fd)


def main(argv: list[str] | None = None) -> int:
    """Run as the detached owner. Returns a process exit status."""
    parser = argparse.ArgumentParser(
        prog="linkedin-mcp-daemon",
        description="Serve one shared LinkedIn browser to local MCP clients.",
    )
    parser.add_argument("--lock-fd", type=int, default=None)
    parser.add_argument("--job-name", default=None)
    args = parser.parse_args(argv)

    def abandon_pending_inherited_lock() -> None:
        if args.lock_fd is None:
            return
        _abandon_inherited_lock(args.lock_fd)
        args.lock_fd = None

    # Standard output is the verdict channel. A private duplicate of standard
    # error carries one fixed, non-secret diagnosis until the real log is ready.
    # The token authenticating verdicts arrives in the configuration, so a
    # failure before that read closes the pipe without emitting a forgeable one.
    bootstrap = _BootstrapDiagnostics(_claim_bootstrap_stream())
    handshake_stream = _claim_handshake_stream()
    try:
        if os.name == "nt":
            if args.job_name is None:
                raise RuntimeError("The Windows owner has no Job Object handoff")
            WindowsJob.verify_current_process(args.job_name)
        # Read before anything else touches the configuration. The frontend
        # closes this pipe after one record; current commit control is separate.
        handover = _read_handover()
    except BaseException:
        # The inherited descriptor shares one POSIX lock with every parent copy.
        # Closing only this copy would leave a suspended starter holding it after
        # this child times out, so a failed handoff explicitly unlocks the shared
        # open-file description before reporting failure.
        abandon_pending_inherited_lock()
        bootstrap.report(BOOTSTRAP_CONFIGURATION)
        _close_handshake_stream(handshake_stream)
        return 1

    config = handover.config
    handshake = _Handshake(handshake_stream, handover.handshake_nonce)
    try:
        set_config(config)
        # Installing the configuration is not enough: the browser mode lives in a
        # module global that defaults to headless (``drivers/browser.py:63``), and
        # ``_make_browser`` reads that global rather than the configuration
        # (``drivers/browser.py:265-283``). The frontend's own entry point sets it
        # explicitly for the same reason (``cli_main.py:391``).
        set_headless(config.browser.headless)
        # Before anything can reach an auth gate. `create_owner_server` records it
        # too, but that runs several steps later, inside `_serve`.
        set_process_role(ServerRole.OWNER)
        profile = Path(config.browser.user_data_dir).expanduser().resolve()
        auth_root = auth_root_dir(profile)
    except BaseException:
        abandon_pending_inherited_lock()
        bootstrap.report(BOOTSTRAP_STATE)
        handshake.abort()
        handshake.close()
        return 1

    if _IS_WINDOWS and not daemon_config.authorizes_commit(handover.startup_protocol):
        abandon_pending_inherited_lock()
        bootstrap.report(BOOTSTRAP_STATE)
        handshake.fail()
        handshake.close()
        return 1

    # Before the first state access, so a rendezvous this process cannot reach
    # costs nothing, and so the parent's authority over an unauthorized child is
    # live for the whole of the startup that follows.
    control: process_control.ControlChannel = sys.stdin
    if handover.control is not None:
        try:
            control = process_control.attach(
                handover.control.host,
                handover.control.port,
                nonce=handover.handshake_nonce,
                timeout=_STARTUP_PROBE_SECONDS,
            )
        except BaseException:
            abandon_pending_inherited_lock()
            bootstrap.report(BOOTSTRAP_CONFIGURATION)
            handshake.abort()
            handshake.close()
            return 1

    try:
        log_path = _attach_daemon_log(auth_root)
    except BaseException:
        abandon_pending_inherited_lock()
        bootstrap.report(BOOTSTRAP_LOG)
        handshake.abort()
        handshake.close()
        _close_owned_control(control)
        return 1
    bootstrap.report(BOOTSTRAP_ATTACHED)

    lock: DaemonLock | None = None
    try:
        configure_logging(log_level=config.server.log_level, json_format=True)
        lock = _take_lock(auth_root, args.lock_fd)
        args.lock_fd = None
        if lock is None:
            logger.info("Another process won the daemon election")
            handshake.retry()
            return 0
        return asyncio.run(
            _serve(
                lock=lock,
                auth_root=auth_root,
                profile=profile,
                config=config,
                log_path=log_path,
                handshake=handshake,
                handshake_nonce=handover.handshake_nonce,
                startup_protocol=handover.startup_protocol,
                control=control,
                job_name=args.job_name,
            )
        )
    except DaemonLockError:
        abandon_pending_inherited_lock()
        logger.exception("The daemon could not take ownership")
        handshake.abort()
        return 1
    except Exception:
        abandon_pending_inherited_lock()
        logger.exception("The daemon stopped with an error")
        handshake.fail()
        return 1
    finally:
        # After the verdict either way, so a frontend blocked on the pipe is
        # released even by a path that forgot to answer.
        bootstrap.close()
        handshake.close()
        _close_owned_control(control)
        abandon_pending_inherited_lock()
        if lock is not None:
            lock.release()


def _close_owned_control(control: process_control.ControlChannel) -> None:
    """Release a control channel this process opened, and never the inherited pipe.

    Standard input belongs to the interpreter and to the predecessor protocols
    that use it; only a channel this process connected for itself is closed here.
    """
    close = getattr(control, "close", None)
    if control is sys.stdin or not callable(close):
        return
    with contextlib.suppress(OSError, ValueError):
        close()


def _read_handover(
    *, timeout: float = _STARTUP_PROBE_SECONDS
) -> daemon_config.OwnerHandover:
    """Read one handover record without letting a suspended parent pin the lock."""
    received: queue.Queue[str | BaseException] = queue.Queue(maxsize=1)

    def read() -> None:
        try:
            received.put(sys.stdin.readline())
        except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
            received.put(exc)

    threading.Thread(target=read, name="daemon-config-read", daemon=True).start()
    try:
        raw = received.get(timeout=max(timeout, 0.0))
    except queue.Empty:
        raise TimeoutError(
            "The daemon starter did not provide its configuration in time"
        ) from None
    if isinstance(raw, BaseException):
        raise raw
    if not raw.strip():
        raise ValueError("The daemon was started without a configuration")
    return daemon_config.decode_handover(raw)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
