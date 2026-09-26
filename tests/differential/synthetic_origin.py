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

Run as a script to issue into a directory: ``python synthetic_origin.py DIR``.
"""

from __future__ import annotations

import datetime
import select
import socket
import ssl
import sys
import threading
from dataclasses import dataclass
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

#: Set only by the CI step that runs a native row, right after a step that
#: trusted the run's CA on a disposable GitHub-hosted runner. Never set it on a
#: workstation: the rows need a trust-store change nothing here will make.
OPT_IN_ENV = "LINKEDIN_MCP_DIFFERENTIAL_CI"

#: Where that CI step issued the run's certificates.
CA_DIR_ENV = "LINKEDIN_MCP_DIFFERENTIAL_CA_DIR"

CA_FILE = "ca.pem"
LEAF_FILE = "leaf.pem"
LEAF_KEY_FILE = "leaf-key.pem"

_FEED_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>Synthetic feed</title></head>"
    f"<body><main id='synthetic-feed'>{FEED_MARKER}</main></body></html>"
).encode()


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


class _OriginHandler(BaseHTTPRequestHandler):
    server: SyntheticOrigin

    def do_GET(self) -> None:
        origin = self.server
        origin.record(
            OriginRequest(
                server_name=getattr(self.connection, "_synthetic_server_name", None),
                host=self.headers.get("Host"),
                path=self.path,
            )
        )
        if self.path.split("?", 1)[0] == "/feed/":
            body, status = _FEED_PAGE, 200
        else:
            body, status = b"", 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
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
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(certificates / LEAF_FILE, certificates / LEAF_KEY_FILE)
        context.set_alpn_protocols(["http/1.1"])
        context.sni_callback = self._remember_server_name
        self._tls = context
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self.server_address[1]

    def record(self, request: OriginRequest) -> None:
        with self._lock:
            self.requests.append(request)

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
