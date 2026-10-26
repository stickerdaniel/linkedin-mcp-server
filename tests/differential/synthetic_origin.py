"""A loopback www.linkedin.com that the real browser can load, and nothing else.

The authentication path always opens ``https://www.linkedin.com/feed/`` and has
no base-URL seam, so a harness that must never reach LinkedIn gives the browser
a LinkedIn of its own instead: the product's ``proxy_server`` setting points at
a loopback proxy, and the proxy tunnels exactly the allowed names to a loopback
HTTPS origin whose certificate a per-run test CA issued.

**The proxy is the egress boundary, so it fails closed.** Every ``CONNECT`` to a
name outside the routes is answered 403, every plain-HTTP request is answered
403, and the only socket it ever opens is to a loopback port a route names. Its
log therefore says what left: a forwarded entry is the whole of the traffic that
reached anything. What it cannot see is traffic that never came to it, such as
a lookup the browser resolved itself; that limit belongs in any claim built on
the log.

**So the real service is fenced off outside the browser as well.** The same CI
step that trusts the CA maps every allowed name, and ``CANARY_HOST``, to
loopback in the runner's hosts file. A browser that stopped using the proxy for
some name would then reach a closed loopback port instead of LinkedIn. The
test checks that mapping from outside the browser before any browser starts,
and checks with the canary that the browser's own resolver honours it.

**Trusting the CA is not this module's business.** It issues the certificates
and serves them; installing the CA into a trust store is a CI step that runs
only on a disposable GitHub-hosted runner. Nothing here touches a store, and
the CA's private key is never written anywhere, so no second certificate can be
minted under it once issuance returns. The CA is also name-constrained to the
allowed names and the names below them, and expires within a day, which bounds
what it vouches for even on the runner that trusts it.

**A request can be held** (``SyntheticOrigin.hold``): one exact path, the
n-th request for it after the gate was armed, kept from any response byte
until the row releases it, its deadline runs out, or its peer is gone. Each of
those ends is recorded apart (``Gate``), and none of them is the browser
having received anything: a write that returned is only bytes handed to the
socket, which on this path is the proxy's tunnel.

**A sign-in can be staged** (``SyntheticOrigin.arm_login``,
``reject_sessions``, ``release_login``). Healthy is the default and serves
every row as before. Once the harness rejects the sessions it accepted, a
``/feed/`` request without an accepted session is answered 302 to the login
wall with the redirect's query, and that wall asks nothing. The wall the
product's own login opens, at exactly ``/login``, is served at once, and its
script asks a completion request again and again, each answered at once;
once the harness released it, that answer sets a fresh synthetic ``li_at``
with ``Set-Cookie`` and the page goes on to the feed. So only a login ever
asks, and only after a release is anything issued. Every session the origin
issued, rejected or was sent is recorded by digest and never by value
(``LoginFixture``).

Run as a script to issue into a directory: ``python synthetic_origin.py DIR``.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import ipaddress
import re
import secrets
import select
import socket
import ssl
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

#: The only names the proxy will tunnel. ``static.licdn.com`` is LinkedIn's
#: asset host; nothing on the synthetic pages loads from it yet.
ALLOWED_HOSTS = ("www.linkedin.com", "static.licdn.com")

#: Mapped to loopback next to the allowed names, and nothing else. Under the
#: reserved ``.test`` domain, so no public name is involved; the CI step supplies
#: the mapping, and a request reaching the canary's listener is the evidence.
CANARY_HOST = "synthetic-canary.test"

#: The subject the CI trust steps look the CA up by when they record the store.
CA_COMMON_NAME = "linkedin-mcp synthetic origin test CA"

#: Present on the synthetic ``/feed/`` page and on nothing LinkedIn serves.
FEED_MARKER = "linkedin-mcp-synthetic-feed-7f3c"

#: In the text of the synthetic feed's one post. ``get_feed`` returns it only
#: if the product's own extractor read the page.
POST_MARKER = "linkedin-mcp-synthetic-post-5d2e"

#: A permalink in the shape ``linkedin.feed_payload.POST_SLUG_URL_RE`` reads out
#: of the ``/feed/`` document. It is never requested: nothing follows it.
SYNTHETIC_POST_URL = (
    "https://www.linkedin.com/posts/synthetic-author-activity-7000000000000000001-synth"
)

#: Set only by the CI step that runs a native row, right after a step that
#: trusted the run's CA on a disposable GitHub-hosted runner. Never set it on a
#: workstation: the rows need a trust-store change nothing here will make.
OPT_IN_ENV = "LINKEDIN_MCP_DIFFERENTIAL_CI"

#: Where that CI step issued the run's certificates.
CA_DIR_ENV = "LINKEDIN_MCP_DIFFERENTIAL_CA_DIR"

CA_FILE = "ca.pem"
LEAF_FILE = "leaf.pem"
LEAF_KEY_FILE = "leaf-key.pem"

_POST_FILLER = (
    "This post exists only on a loopback origin the differential harness "
    "serves, so that the feed tool has something of its own to read. "
) * 3

# The least a signed-in feed needs for the product's checks, element by element.
# No asset, script or frame, so nothing on it sends the browser anywhere but
# this origin (at most a favicon lookup here), and the fence around the real
# names stays the whole boundary the rows need.
#
# * The title is not one ``core.auth._LOGIN_TITLE_PATTERNS`` names, and the URL
#   is not an auth blocker, so ``_detect_auth_barrier`` (quick and full, used by
#   ``drivers.browser._feed_auth_succeeds`` and by the navigator) finds nothing.
# * No ``#rememberme-div``: ``resolve_remember_me_prompt`` times out and returns
#   False, and the barrier check has no account picker to report.
# * The body text avoids every ``_AUTH_BARRIER_TEXT_MARKERS`` pair.
# * ``nav a[href*="/feed"]`` is the selector ``core.auth.is_logged_in`` takes
#   as signed in; the URL fallback there would also accept the non-empty body.
# * ``<main>`` makes ``detect_rate_limit`` skip its body-text heuristic, and is
#   what ``FeedReader`` waits for and reads; its text is over the 200
#   characters that end ``FeedReader``'s content wait at once.
# * The permalink sits in the document itself, which ``FeedReader`` reads for
#   ``POST_SLUG_URL_RE`` because ``/feed/`` is a feed payload URL, so one post
#   is captured before the first scroll and the scroll loop stops there.
_FEED_PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Feed | LinkedIn</title></head><body>"
    "<nav aria-label='Primary'><a href='/feed/'>Home</a></nav>"
    f"<main id='synthetic-feed'><p>{FEED_MARKER}</p>"
    f"<article><a href='{SYNTHETIC_POST_URL}'>Synthetic Author</a>"
    f"<p>{POST_MARKER} {_POST_FILLER}</p></article></main>"
    "</body></html>"
).encode()

#: The person sections the origin serves, by the product's section name, with
#: the suffix ``linkedin.fields.PERSON_SECTIONS`` navigates for each under
#: ``/in/<username>``. Written out rather than imported, because this module
#: also runs as a CI script that issues certificates; a unit test holds the
#: two equal.
PERSON_PAGES = {
    "main_profile": "/",
    "experience": "/details/experience/",
    "education": "/details/education/",
}

#: In each person page's text and on nothing LinkedIn serves, so a section the
#: product returns is shown to come from its own page.
PERSON_MARKERS = {
    "main_profile": "linkedin-mcp-synthetic-person-4a1b",
    "experience": "linkedin-mcp-synthetic-experience-8c3d",
    "education": "linkedin-mcp-synthetic-education-2e9f",
}

#: A username a row may choose: the shape ``person_profile_url`` passes through
#: unescaped, so each one is exactly one path segment and two different ones
#: are two different paths.
_PERSON_PATH = re.compile(
    r"^/in/(?P<username>[a-z0-9-]{3,100})"
    r"(?P<suffix>/|/details/experience/|/details/education/)$"
)

_PERSON_FILLER = (
    "This profile exists only on a loopback origin the differential harness "
    "serves, so that the person tool has something of its own to read."
)

_PERSON_HEADINGS = {
    "main_profile": "Synthetic Person",
    "experience": "Experience",
    "education": "Education",
}


def person_path(username: str, section: str) -> str:
    """The path the product navigates for *section* of *username*'s profile."""
    return f"/in/{username}{PERSON_PAGES[section]}"


def _person_page(section: str) -> bytes:
    """The least a person section needs, derived from the product's checks.

    * The title is no ``core.auth._LOGIN_TITLE_PATTERNS`` entry and the URL no
      auth blocker, so the quick barrier check after each navigation passes;
      no ``#rememberme-div`` and none of the barrier text pairs.
    * ``<main>`` is what ``SectionCapture`` waits for and reads, and makes
      ``detect_rate_limit`` skip its body-text heuristic.
    * A detail page's text starts with its heading, never with one of
      ``DETAIL_CAPTURE_EN_US.readiness_blocking_prefixes``, so the details
      readiness wait ends at once; it has no button, so no "Show more" click.
    * Nothing matches ``linkedin.text._NOISE_MARKERS``, so the section is not
      read as chrome only, and the page has no top-card compose link, so the
      profile URN read finds none and moves on.

    No asset, script or frame, as on the feed page.
    """
    heading = _PERSON_HEADINGS[section]
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{heading} | LinkedIn</title></head><body>"
        f"<main><h1>{heading}</h1><p>{PERSON_MARKERS[section]}</p>"
        f"<p>{_PERSON_FILLER}</p></main>"
        "</body></html>"
    ).encode()


def page_for(path: str) -> tuple[bytes, int]:
    """The body and status the origin answers *path* with."""
    path = path.split("?", 1)[0]
    if path == "/feed/":
        return _FEED_PAGE, 200
    match = _PERSON_PATH.match(path)
    if match is not None:
        suffix = match.group("suffix")
        section = next(name for name, end in PERSON_PAGES.items() if end == suffix)
        return _person_page(section), 200
    return b"", 404


# --- Signing in ----------------------------------------------------------------

#: The wall the product's login opens (``setup._run_login``), and where a
#: rejected ``/feed/`` sends the browser. ``core.auth`` reads both the path
#: and the title as an auth barrier.
LOGIN_PATH = "/login"
#: Where a rejected ``/feed/`` is sent, in the shape of LinkedIn's own: the
#: wall's path, which every auth check reads, with the page it came from.
LOGIN_REDIRECT = f"{LOGIN_PATH}?session_redirect=%2Ffeed%2F"
#: The wall's completion request: answered at once, every time.
LOGIN_POLL_PATH = "/synthetic-login/poll"
#: On the wall and on nothing LinkedIn serves.
LOGIN_MARKER = "linkedin-mcp-synthetic-login-6a4d"
#: How long the wall's script waits between two asks.
LOGIN_POLL_INTERVAL_SECONDS = 0.5
#: How long the script gives one ask before it aborts it and asks again. The
#: origin answers each at once, so this is only the bound a lost answer
#: meets, and it sits below both limits a request here meets: the product's
#: 30 s navigation timeout and the proxy relay's 30 s idle cut
#: (``_RELAY_IDLE_SECONDS``).
LOGIN_POLL_TIMEOUT_SECONDS = 5.0
#: How long an issued ``li_at`` lives: as long as the staged one
#: (``session.synthetic_cookies``).
ISSUED_SESSION_SECONDS = 30 * 24 * 60 * 60
#: The prefix of every value the origin issues. The value itself never
#: leaves this process but in the ``Set-Cookie`` answer to the browser.
ISSUED_PREFIX = "synthetic-issued-"

#: Who let the sign-in complete: the row, never the teardown, which closes
#: it instead (``SyntheticOrigin.close_login``).
COMPLETED_BY_ROW = "row"

# The least a login wall needs, derived from the product's login: the title is
# one ``core.auth._LOGIN_TITLE_PATTERNS`` names and the path an auth blocker;
# no ``#rememberme-div``, so ``resolve_remember_me_prompt`` gives up after its
# own wait and ``wait_for_manual_login`` reads the context's cookies. The
# script asks one completion request at a time; the answer that issues a
# session carries it in ``Set-Cookie``, which a same-origin fetch stores, and
# the page then goes on to the feed as a signed-in LinkedIn would.
_LOGIN_PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Sign In | LinkedIn</title></head><body>"
    f"<main><h1>Sign in</h1><p>{LOGIN_MARKER}</p></main>"
    "<script>(async () => {"
    "for (;;) {"
    "let issued = false;"
    "const controller = new AbortController();"
    "const timer = setTimeout(() => controller.abort(), "
    f"{int(LOGIN_POLL_TIMEOUT_SECONDS * 1000)});"
    "try {"
    f"const answer = await fetch('{LOGIN_POLL_PATH}', "
    "{cache: 'no-store', credentials: 'same-origin', signal: controller.signal});"
    "issued = answer.status === 200;"
    "} catch (error) {}"
    "clearTimeout(timer);"
    "if (issued) { location.replace('/feed/'); return; }"
    "await new Promise((resolve) => setTimeout(resolve, "
    f"{int(LOGIN_POLL_INTERVAL_SECONDS * 1000)}));"
    "}"
    "})();</script>"
    "</body></html>"
).encode()

#: The wall a redirected browser sees: the same barrier, and no script.
_REDIRECTED_WALL = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<title>Sign In | LinkedIn</title></head><body>"
    f"<main><h1>Sign in</h1><p>{LOGIN_MARKER}</p></main>"
    "</body></html>"
).encode()

#: How each completion ask was answered. ``pending``: not released yet.
#: ``issued``: a fresh session, the first for this browser. ``already``: the
#: browser already sends one this origin issued, so nothing new. ``closed``:
#: asked after the row closed the sign-in.
PENDING = "pending"
ISSUED = "issued"
ALREADY = "already"
CLOSED = "closed"


def session_digest(value: str | bytes) -> str:
    """The digest a session is recorded by; the value never is."""
    raw = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


@dataclass
class LoginFixture:
    """What the origin's sign-in saw, by digest and monotonic time only."""

    #: The wall and its completion request are served.
    armed: bool = False
    armed_ns: int | None = None
    #: Each rejection the harness made: the digests it took back, and when.
    rejections: list[dict[str, Any]] = field(default_factory=list)
    #: Each request for ``LOGIN_PATH``, and each ``/feed/`` sent there.
    walls: list[int] = field(default_factory=list)
    redirects: list[int] = field(default_factory=list)
    #: Each completion ask: when, and how it was answered.
    polls: list[tuple[int, str]] = field(default_factory=list)
    released_ns: int | None = None
    released_by: str | None = None
    #: The phase the release named, carried by every session it issued.
    released_phase: str | None = None
    closed_ns: int | None = None
    #: Each session issued: its digest, when, the phase, its ordinal.
    issued: list[dict[str, Any]] = field(default_factory=list)

    def as_record(self) -> dict[str, Any]:
        pending = [at for at, answer in self.polls if answer == PENDING]
        return {
            "armed": self.armed,
            "armed_ns": self.armed_ns,
            "rejections": [dict(item) for item in self.rejections],
            "walls": list(self.walls),
            "redirects": list(self.redirects),
            "polls": len(self.polls),
            "first_poll_ns": self.polls[0][0] if self.polls else None,
            "last_poll_ns": self.polls[-1][0] if self.polls else None,
            "last_pending_ns": pending[-1] if pending else None,
            "answers": {
                answer: sum(1 for _, seen in self.polls if seen == answer)
                for answer in (PENDING, ISSUED, ALREADY, CLOSED)
            },
            "released_ns": self.released_ns,
            "released_by": self.released_by,
            "released_phase": self.released_phase,
            "closed_ns": self.closed_ns,
            "issued": [dict(item) for item in self.issued],
        }


def _issued_cookie(value: str) -> str:
    """The ``Set-Cookie`` value an issued session is answered with: the
    domain the product stores LinkedIn's cookies under, an expiry, and the
    flags a signed-in LinkedIn session carries."""
    return (
        f"li_at={value}; Domain=.linkedin.com; Path=/; "
        f"Max-Age={ISSUED_SESSION_SECONDS}; Secure; HttpOnly; SameSite=None"
    )


# --- Holding one request -------------------------------------------------------

#: How long a held request may wait, counted from its entry into the gate.
#: Below both limits a held request meets, both counted from that request and
#: not from the ``goto`` call: the product's navigation budget
#: (``NAVIGATION_BUDGET_MS``, 30s, in ``core.proxy_errors``) and this module's
#: proxy relay, which ends a tunnel idle for ``_RELAY_IDLE_SECONDS`` (30). A
#: held request sends nothing through its tunnel, so a hold of 30 s would be
#: cut by the relay rather than ended by the gate; 20 leaves the answer ten
#: seconds to arrive. The time a fresh browser spends before it sends the
#: request is not part of either limit.
GATE_DEADLINE_SECONDS = 20.0
#: How often a held request looks for its peer having gone.
_PEER_POLL_SECONDS = 0.05

#: How a hold ended. ``served``: the row released it before its deadline and
#: the answer was written. ``deadline``: the deadline ran out first; the answer
#: is written anyway, so the read is not left to the navigation timeout, and
#: ``wrote`` says whether that write returned. ``peer-gone``: the peer was
#: seen gone while held, or writing the released answer failed. A write that
#: returned is not the browser's receipt: it went into the proxy's tunnel.
SERVED = "served"
DEADLINE = "deadline"
PEER_GONE = "peer-gone"

#: Who let a held request go: the row, or the teardown releasing whatever is
#: still held.
RELEASED_BY_ROW = "row"
RELEASED_BY_TEARDOWN = "teardown"

GateEvent = Callable[..., Any]


class Gate:
    """One armed hold: the *ordinal*-th request for *path* after arming.

    Its fields are written by the handler thread that holds the request and
    by whoever releases it, each under the gate's own lock; the origin's lock
    is never held while a request waits.
    """

    def __init__(
        self,
        path: str,
        ordinal: int,
        seconds: float,
        on_event: GateEvent | None,
    ) -> None:
        self.path = path
        self.ordinal = ordinal
        self.seconds = seconds
        self._on_event = on_event
        self._lock = threading.Lock()
        self._seen = 0
        self._release = threading.Event()
        #: Set once the request entered, before any byte of its answer.
        self.entered = threading.Event()
        #: Set once the hold's terminal is recorded.
        self.ended = threading.Event()
        self.entered_monotonic_ns: int | None = None
        self.release_requested_monotonic_ns: int | None = None
        self.released_by: str | None = None
        #: When the hold let the request go, before any byte of its answer
        #: was written: whatever the peer asks next arrives after this.
        self.released_monotonic_ns: int | None = None
        self.terminal: str | None = None
        self.wrote: bool | None = None
        #: Reporting an event failed; the hold went on regardless.
        self.event_errors: list[str] = []

    def _selects(self) -> bool:
        """Whether this request is the one the gate holds; counted once each."""
        with self._lock:
            if self.entered.is_set() or self._seen >= self.ordinal:
                return False
            self._seen += 1
            return self._seen == self.ordinal

    def release(self, *, by: str = RELEASED_BY_ROW) -> None:
        """Let the held request go, or the one still to come at once.

        The first release names who released; a later one changes nothing.
        """
        with self._lock:
            if self.released_by is None:
                self.released_by = by
                self.release_requested_monotonic_ns = time.monotonic_ns()
        self._release.set()

    def _report(self, kind: str, **fields: Any) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, **fields)
        except Exception as exc:  # noqa: BLE001 - recorded; the hold goes on
            self.event_errors.append(f"{kind}: {type(exc).__name__}: {exc}")

    def _enter(self) -> None:
        with self._lock:
            self.entered_monotonic_ns = time.monotonic_ns()
        self.entered.set()
        self._report(
            "gate.entered",
            path=self.path,
            ordinal=self.ordinal,
            entered_monotonic_ns=self.entered_monotonic_ns,
        )

    def _wait(self, connection: Any) -> str | None:
        """Hold until released (None), the deadline, or the peer gone, and
        record when the hold let go."""
        ended = self._hold(connection)
        with self._lock:
            self.released_monotonic_ns = time.monotonic_ns()
        return ended

    def _hold(self, connection: Any) -> str | None:
        assert self.entered_monotonic_ns is not None
        deadline = self.entered_monotonic_ns + int(self.seconds * 1e9)
        while True:
            remaining = (deadline - time.monotonic_ns()) / 1e9
            if remaining <= 0:
                return DEADLINE
            if self._release.wait(min(remaining, _PEER_POLL_SECONDS)):
                return None
            if peer_gone(connection):
                return PEER_GONE

    def _finish(self, terminal: str, *, wrote: bool) -> None:
        with self._lock:
            self.terminal = terminal
            self.wrote = wrote
        self.ended.set()
        self._report(
            "gate.released",
            path=self.path,
            ordinal=self.ordinal,
            entered_monotonic_ns=self.entered_monotonic_ns,
            released_monotonic_ns=self.released_monotonic_ns,
            terminal=terminal,
            released_by=self.released_by,
            release_requested_monotonic_ns=self.release_requested_monotonic_ns,
            wrote=wrote,
            deadline_seconds=self.seconds,
        )

    def as_record(self) -> dict[str, Any]:
        """What the gate observed, fit for a row's packet."""
        with self._lock:
            return {
                "path": self.path,
                "ordinal": self.ordinal,
                "deadline_seconds": self.seconds,
                "entered_monotonic_ns": self.entered_monotonic_ns,
                "release_requested_monotonic_ns": (self.release_requested_monotonic_ns),
                "released_by": self.released_by,
                "released_monotonic_ns": self.released_monotonic_ns,
                "terminal": self.terminal,
                "wrote": self.wrote,
                "event_errors": list(self.event_errors),
            }


def peer_gone(connection: Any) -> bool:
    """Whether the peer of a held request has gone, without blocking.

    Only asked while the request waits for its answer, when nothing should
    arrive: an end of stream, a reset or a failed read is the peer gone. A
    byte that does arrive is consumed and the request goes on waiting; the
    gate closes the connection after answering, so nothing reads past it.
    """
    try:
        readable, _, broken = select.select([connection], [], [connection], 0)
    except (OSError, ValueError):
        return True
    if broken:
        return True
    pending = getattr(connection, "pending", None)
    if not readable and not (pending is not None and pending()):
        return False
    previous = connection.gettimeout()
    try:
        connection.setblocking(False)
        data = connection.recv(1)
    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
        return False
    except OSError:
        return True
    finally:
        try:
            connection.settimeout(previous)
        except OSError:
            pass
    return data == b""


def fence_breaches(hosts: tuple[str, ...] | None = None) -> dict[str, list[str]]:
    """Each fenced name the operating system resolves to anything but loopback.

    Empty is the fence holding. An unresolvable name is a breach as well: the CI
    step maps every one of them, so a name it cannot resolve is a step that did
    not run.
    """
    breaches: dict[str, list[str]] = {}
    for host in hosts or (*ALLOWED_HOSTS, CANARY_HOST):
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            breaches[host] = [f"unresolved ({error})"]
            continue
        outside = sorted(
            {
                str(info[4][0])
                for info in infos
                if not ipaddress.ip_address(info[4][0]).is_loopback
            }
        )
        if outside:
            breaches[host] = outside
    return breaches


def cookie_names(header: str | None) -> tuple[str, ...]:
    """The cookie names in a ``Cookie`` header. Values are never kept."""
    if not header:
        return ()
    names = {part.split("=", 1)[0].strip() for part in header.split(";") if "=" in part}
    return tuple(sorted(name for name in names if name))


def cookie_values(header: str | None, name: str) -> list[str]:
    """Every value *header* sends under *name*, for comparison and nothing else."""
    if not header:
        return []
    values = []
    for part in header.split(";"):
        key, separator, value = part.partition("=")
        if separator and key.strip() == name:
            values.append(value.strip())
    return values


def issue_certificates(
    directory: Path, *, ca_common_name: str = CA_COMMON_NAME
) -> None:
    """Write a fresh CA certificate and a leaf for ``ALLOWED_HOSTS``.

    Writes ``ca.pem`` (certificate only), ``leaf.pem`` and ``leaf-key.pem``.
    The CA key lives only in this call.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    # An hour back, so a runner clock slightly behind the issuing one does not
    # read the certificate as not yet valid.
    not_before = now - datetime.timedelta(hours=1)
    not_after = now + datetime.timedelta(days=1)
    names = [x509.DNSName(host) for host in ALLOWED_HOSTS]

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ca_common_name)])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        # A trusted root that can vouch only for the allowed names and the
        # names below them, since DNS constraints are subtrees (RFC 5280).
        # Chromium enforces constraints carried by a locally trusted anchor.
        .add_extension(
            x509.NameConstraints(permitted_subtrees=names, excluded_subtrees=None),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf_cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, ALLOWED_HOSTS[0])])
        )
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )

    directory.mkdir(parents=True, exist_ok=True)
    (directory / CA_FILE).write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (directory / LEAF_FILE).write_bytes(
        leaf_cert.public_bytes(serialization.Encoding.PEM)
    )
    (directory / LEAF_KEY_FILE).write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


@dataclass(frozen=True)
class OriginRequest:
    server_name: str | None
    host: str | None
    path: str
    #: Names only, from the ``Cookie`` header: which session the browser sent.
    cookie_names: tuple[str, ...] = ()
    #: Wall-clock arrival, comparable with the harness's event log.
    t: float = 0.0
    #: Whether the request's ``li_at`` is a session this origin accepts. None
    #: while it accepts none. The value itself is compared, never recorded.
    session_valid: bool | None = None
    #: Arrival on the harness's monotonic clock, which the origin shares with
    #: the host stub's call intervals because both run in the harness's own
    #: process. None for a request recorded without one.
    monotonic_ns: int | None = None
    #: The digest of every ``li_at`` the request sent, never its value: which
    #: session, the staged one or one the origin issued, it carried.
    session_digests: tuple[str, ...] = ()
    #: Answered 302 to the login wall: a ``/feed/`` without an accepted
    #: session once the harness rejected the sessions it had accepted.
    redirected: bool = False


class _OriginHandler(BaseHTTPRequestHandler):
    server: SyntheticOrigin

    def do_GET(self) -> None:
        origin = self.server
        header = self.headers.get("Cookie")
        path = self.path.split("?", 1)[0]
        valid = origin.judge_session(header)
        walled = origin.walls(path, valid)
        origin.record(
            OriginRequest(
                server_name=getattr(self.connection, "_synthetic_server_name", None),
                host=self.headers.get("Host"),
                path=self.path,
                cookie_names=cookie_names(header),
                t=time.time(),
                session_valid=valid,
                monotonic_ns=time.monotonic_ns(),
                session_digests=tuple(
                    session_digest(value) for value in cookie_values(header, "li_at")
                ),
                redirected=walled,
            )
        )
        if walled or origin.serves_login(path):
            self._sign_in(origin, path, header, walled=walled, query="?" in self.path)
            return
        gate = origin.gate_for(path)
        ended: str | None = None
        if gate is not None:
            # Entered before any byte of the answer, and waited on with no lock
            # of the origin's held, so every other request is served meanwhile.
            gate._enter()
            ended = gate._wait(self.connection)
            if ended == PEER_GONE:
                gate._finish(PEER_GONE, wrote=False)
                self.close_connection = True
                return
        body, status = page_for(self.path)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            if gate is None:
                raise
            gate._finish(ended or PEER_GONE, wrote=False)
            self.close_connection = True
            return
        if gate is not None:
            gate._finish(ended or SERVED, wrote=True)
            # A byte the hold consumed while looking for its peer would be
            # read as the start of the next request.
            self.close_connection = True

    def _sign_in(
        self,
        origin: SyntheticOrigin,
        path: str,
        header: str | None,
        *,
        walled: bool,
        query: bool,
    ) -> None:
        """The wall, a rejected feed's way to it, and the completion ask,
        each answered at once.

        Only the product's login opens the wall itself, at exactly
        ``LOGIN_PATH``; a browser a rejected feed sent there arrives with the
        redirect's query and is served a wall that asks for nothing, so no
        ask, and no session, ever comes from anything but a login.
        """
        cookie: str | None = None
        if walled:
            origin.note_wall(redirect=True)
            status, body = 302, b""
        elif path == LOGIN_PATH and query:
            status, body = 200, _REDIRECTED_WALL
        elif path == LOGIN_PATH:
            origin.note_wall()
            status, body = 200, _LOGIN_PAGE
        else:
            answer, cookie = origin.complete_login(header)
            status = 200 if answer in (ISSUED, ALREADY) else 204
            body = answer.encode() if status == 200 else b""
        self.send_response(status)
        if walled:
            self.send_header("Location", LOGIN_REDIRECT)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        if status != 204:
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class SyntheticOrigin(ThreadingHTTPServer):
    """A loopback HTTPS server presenting the leaf in *certificates*."""

    daemon_threads = True

    def __init__(self, certificates: Path) -> None:
        super().__init__(("127.0.0.1", 0), _OriginHandler)
        self._lock = threading.Lock()
        self.requests: list[OriginRequest] = []
        #: Connections that failed before a request could be read. A browser
        #: that rejects the certificate aborts the handshake, and this is where
        #: that shows up on the server side.
        self.failures: list[str] = []
        self._sessions: list[bytes] = []
        #: Sessions the harness took back (``reject_sessions``).
        self._rejected: list[bytes] = []
        #: Sessions this origin issued itself (``complete_login``).
        self._issued: list[bytes] = []
        self._login = LoginFixture()
        self._gates: list[Gate] = []
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certificates / LEAF_FILE, certificates / LEAF_KEY_FILE)
        context.set_alpn_protocols(["http/1.1"])
        context.sni_callback = self._remember_server_name
        self._tls = context
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def accept_session(self, li_at: str) -> None:
        """Treat *li_at* as a session this origin issued.

        Held in memory only. The origin refreshes no session of its own, so
        the staged value is the only one a row's browser may send, until a
        row stages a sign-in and the origin issues a fresh one
        (``complete_login``).
        """
        with self._lock:
            self._sessions.append(li_at.encode())

    def judge_session(self, header: str | None) -> bool | None:
        """Whether *header* sends a session this origin accepts now; None
        while it has never accepted or rejected any."""
        with self._lock:
            sessions = list(self._sessions)
            judged = bool(sessions or self._rejected)
        if not judged:
            return None
        sent = [value.encode() for value in cookie_values(header, "li_at")]
        return any(
            hmac.compare_digest(value, session)
            for value in sent
            for session in sessions
        )

    def accepts_digest(self, digest: str) -> bool:
        """Whether the session with *digest* is one this origin accepts now:
        the origin's own judgement of a session on disk, read without any
        browser asking it."""
        with self._lock:
            sessions = list(self._sessions)
        return any(session_digest(session) == digest for session in sessions)

    # --- Signing in ----------------------------------------------------------

    def arm_login(self) -> None:
        """Serve the login wall and its completion request from now on."""
        with self._lock:
            if not self._login.armed:
                self._login.armed = True
                self._login.armed_ns = time.monotonic_ns()

    def reject_sessions(self) -> dict[str, Any]:
        """The harness's own rejection of every session accepted so far.

        From here a ``/feed/`` without an accepted session is answered 302 to
        the wall, which is served, as is the completion request. What is
        taken back, and when, is recorded by digest; the time is read under
        the same lock that takes them, so no request is judged in between.
        """
        with self._lock:
            taken = list(self._sessions)
            self._sessions.clear()
            self._rejected.extend(taken)
            at = time.monotonic_ns()
            if not self._login.armed:
                self._login.armed = True
                self._login.armed_ns = at
            found = {
                "digests": [session_digest(session) for session in taken],
                "monotonic_ns": at,
            }
            self._login.rejections.append(found)
        return dict(found)

    def walls(self, path: str, valid: bool | None) -> bool:
        """Whether a request for *path* judged *valid* meets the wall."""
        with self._lock:
            rejected = bool(self._rejected)
        return path == "/feed/" and rejected and valid is not True

    def serves_login(self, path: str) -> bool:
        with self._lock:
            armed = self._login.armed
        return armed and path in (LOGIN_PATH, LOGIN_POLL_PATH)

    def note_wall(self, *, redirect: bool = False) -> None:
        """A request served the wall, or (*redirect*) sent there."""
        with self._lock:
            seen = self._login.redirects if redirect else self._login.walls
            seen.append(time.monotonic_ns())

    def release_login(
        self, *, by: str = COMPLETED_BY_ROW, phase: str = "row"
    ) -> dict[str, Any]:
        """Let the next completion asks issue fresh sessions. The first
        release names who released and the phase every issue carries."""
        with self._lock:
            if self._login.released_ns is None:
                self._login.released_ns = time.monotonic_ns()
                self._login.released_by = by
                self._login.released_phase = phase
            return {
                "released_ns": self._login.released_ns,
                "released_by": self._login.released_by,
                "phase": self._login.released_phase,
            }

    def close_login(self) -> None:
        """No session is issued from now on, released or not: the teardown's,
        so nothing still asking after the row can sign in."""
        with self._lock:
            if self._login.closed_ns is None:
                self._login.closed_ns = time.monotonic_ns()

    def complete_login(self, header: str | None) -> tuple[str, str | None]:
        """One completion ask: how it is answered, and the ``Set-Cookie``
        value when it issues a session. Decided and recorded under one lock,
        so two asks never both issue for one release."""
        sent = [value.encode() for value in cookie_values(header, "li_at")]
        with self._lock:
            login = self._login
            at = time.monotonic_ns()
            if any(
                hmac.compare_digest(value, issued)
                for value in sent
                for issued in self._issued
            ):
                login.polls.append((at, ALREADY))
                return ALREADY, None
            if login.closed_ns is not None:
                login.polls.append((at, CLOSED))
                return CLOSED, None
            if login.released_ns is None:
                login.polls.append((at, PENDING))
                return PENDING, None
            value = f"{ISSUED_PREFIX}{secrets.token_urlsafe(24)}"
            self._issued.append(value.encode())
            self._sessions.append(value.encode())
            login.polls.append((at, ISSUED))
            login.issued.append(
                {
                    "digest": session_digest(value),
                    "issued_ns": at,
                    "phase": login.released_phase,
                    "ordinal": len(login.issued) + 1,
                }
            )
        return ISSUED, _issued_cookie(value)

    def login_record(self) -> dict[str, Any]:
        """What the sign-in saw so far, fit for a row's packet."""
        with self._lock:
            return self._login.as_record()

    def record(self, request: OriginRequest) -> None:
        with self._lock:
            self.requests.append(request)

    def hold(
        self,
        path: str,
        *,
        ordinal: int = 1,
        seconds: float = GATE_DEADLINE_SECONDS,
        on_event: GateEvent | None = None,
    ) -> Gate:
        """Arm a gate for the *ordinal*-th request of exactly *path* from now.

        *path* is matched whole, without its query, never as a prefix.
        *seconds* is the hold's deadline from entry, at most
        ``GATE_DEADLINE_SECONDS``. *on_event* is told ``gate.entered`` and
        ``gate.released`` with their fields, from the handler's thread.
        """
        if type(ordinal) is not int or ordinal < 1:
            raise ValueError(f"a gate's ordinal counts from 1, not {ordinal!r}")
        if not 0 < seconds <= GATE_DEADLINE_SECONDS:
            raise ValueError(
                f"a gate's deadline must be above 0 and at most "
                f"{GATE_DEADLINE_SECONDS}s, below the navigation and relay "
                f"limits, not {seconds!r}"
            )
        gate = Gate(path, ordinal, seconds, on_event)
        with self._lock:
            self._gates.append(gate)
        return gate

    def gate_for(self, path: str) -> Gate | None:
        """The armed gate that holds this request of *path*, if any."""
        with self._lock:
            gates = [gate for gate in self._gates if gate.path == path]
        for gate in gates:
            if gate._selects():
                return gate
        return None

    def release_all(self) -> None:
        """Let every held request go, and every armed gate pass at once."""
        with self._lock:
            gates = list(self._gates)
        for gate in gates:
            gate.release(by=RELEASED_BY_TEARDOWN)

    @staticmethod
    def _remember_server_name(
        connection: Any, server_name: str | None, _context: ssl.SSLContext
    ) -> None:
        connection._synthetic_server_name = server_name

    def get_request(self) -> tuple[Any, Any]:
        # Wrapped per connection with the handshake deferred to the handler
        # thread. Wrapping the listening socket would handshake inside
        # accept(), on the one thread that serves every connection, so a client
        # that stalls mid-handshake would stall the origin with it.
        connection, address = self.socket.accept()
        return (
            self._tls.wrap_socket(
                connection, server_side=True, do_handshake_on_connect=False
            ),
            address,
        )

    def handle_error(self, request: Any, client_address: Any) -> None:
        with self._lock:
            self.failures.append(repr(sys.exc_info()[1]))

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # First: a request still held would otherwise wait out its deadline.
        self.release_all()
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


@dataclass(frozen=True)
class ProxyDecision:
    method: str
    target: str
    host: str
    port: int | None
    forwarded: bool


_RELAY_IDLE_SECONDS = 30


class _ProxyHandler(BaseHTTPRequestHandler):
    server: EgressProxy
    protocol_version = "HTTP/1.1"
    # Unbuffered, so reading the CONNECT head cannot swallow the first bytes of
    # the tunnel into a buffer the relay below never sees.
    rbufsize = 0

    def do_CONNECT(self) -> None:
        host, _, port_text = self.path.rpartition(":")
        try:
            port: int | None = int(port_text)
        except ValueError:
            port = None
        upstream_port = self.server.route_for(host, port)
        if upstream_port is None:
            self.server.record(
                ProxyDecision("CONNECT", self.path, host.lower(), port, False)
            )
            self._refuse()
            return
        # The one outbound connection the proxy makes, always to loopback.
        upstream = socket.create_connection(("127.0.0.1", upstream_port), timeout=10)
        self.server.record(
            ProxyDecision("CONNECT", self.path, host.lower(), port, True)
        )
        try:
            self.send_response(200, "Connection Established")
            self.end_headers()
            self._relay(upstream)
        finally:
            upstream.close()
            self.close_connection = True

    def _relay(self, upstream: socket.socket) -> None:
        client = self.connection
        peers = {client: upstream, upstream: client}
        while True:
            readable, _, broken = select.select(
                list(peers), [], list(peers), _RELAY_IDLE_SECONDS
            )
            if broken or not readable:
                return
            for source in readable:
                try:
                    data = source.recv(65536)
                    if not data:
                        return
                    peers[source].sendall(data)
                except OSError:
                    return

    def _refuse_plain(self) -> None:
        target = urlsplit(self.path)
        self.server.record(
            ProxyDecision(
                self.command,
                self.path,
                (target.hostname or "").lower(),
                target.port,
                False,
            )
        )
        self._refuse()

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = (
        _refuse_plain
    )

    def _refuse(self) -> None:
        self.send_response(403, "Egress refused by the synthetic origin proxy")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        pass


class EgressProxy(ThreadingHTTPServer):
    """A loopback HTTP proxy that tunnels routed names and refuses the rest."""

    daemon_threads = True

    def __init__(self, routes: dict[str, int]) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self._lock = threading.Lock()
        self._routes = {host.lower(): port for host, port in routes.items()}
        self.decisions: list[ProxyDecision] = []
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def route(self, host: str, upstream_port: int) -> None:
        if host.lower() not in ALLOWED_HOSTS:
            raise ValueError(f"{host} is not a synthetic origin name")
        with self._lock:
            self._routes[host.lower()] = upstream_port

    def route_for(self, host: str, port: int | None) -> int | None:
        if port != 443:
            return None
        with self._lock:
            return self._routes.get(host.lower())

    def record(self, decision: ProxyDecision) -> None:
        with self._lock:
            self.decisions.append(decision)

    def forwarded(self) -> list[ProxyDecision]:
        with self._lock:
            return [decision for decision in self.decisions if decision.forwarded]

    def refused(self) -> list[ProxyDecision]:
        with self._lock:
            return [decision for decision in self.decisions if not decision.forwarded]

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: synthetic_origin.py DIRECTORY")
    issue_certificates(Path(sys.argv[1]))
