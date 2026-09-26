"""Can the bundled browser load a synthetic www.linkedin.com at all?

The first STOP gate of the native differential harness. Every browser row after
this one assumes that the product's own launch, with nothing but
``proxy_server`` pointed at a loopback proxy, accepts a certificate from a
per-run CA the runner's trust store holds. Nothing in the source can promise
that: which store the bundled Chromium reads is a fact about each platform's
build, so this measures it on each CI leg before anything depends on it.

The real service is protected twice, and the test proves both before it names
it. First, outside any browser: the operating system must resolve every
synthetic name to loopback only, which the CI step arranges in the hosts file,
and an unproxied browser must reach a loopback listener through the canary
name, which shows its own resolver reads that file. A browser that bypassed the
proxy would then find a closed loopback port rather than LinkedIn.

Then three requests through the proxy. The first goes to a reserved
``.invalid`` name and must be refused *by the proxy*, within a deadline; if the
proxy never hears of it, the browser is not using the proxy and the test stops
before it names www.linkedin.com. It is a ``fetch`` rather than a navigation: a
failed navigation commits its error page later, and that commit interrupted the
next ``goto`` when measured. The second loads the synthetic feed. The third
sends ``static.licdn.com`` to an origin whose CA nobody trusts, which must fail
with an authority error: without that, a pass could also mean certificate
checking is off.

Every browser the test starts must also confirm its shutdown.

Runs only where CI opted in after trusting the CA. See ``synthetic_origin``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID
from patchright.async_api import Error as PlaywrightError
from patchright.async_api import async_playwright

from differential.synthetic_origin import (
    ALLOWED_HOSTS,
    CA_COMMON_NAME,
    CA_FILE,
    CANARY_HOST,
    FEED_MARKER,
    OPT_IN_ENV,
    EgressProxy,
    SyntheticOrigin,
    issue_certificates,
)
from linkedin_mcp_server.browser_launch import build_launch_options
from linkedin_mcp_server.config.schema import BrowserConfig
from linkedin_mcp_server.core.browser import BrowserManager

pytestmark = [
    pytest.mark.differential_browser,
    pytest.mark.xdist_group("browser_runtime"),
    pytest.mark.skipif(
        os.environ.get(OPT_IN_ENV) != "1",
        reason=(
            f"native differential row: needs a per-run test CA that only a "
            f"disposable CI runner trusts, so it runs only where the CI step "
            f"sets {OPT_IN_ENV}=1 after installing that CA. Do not set it "
            f"locally."
        ),
    ),
]

_NAVIGATION_TIMEOUT_MS = 30_000
_CONTROL_TIMEOUT_MS = 10_000
# Beyond the browser's own abort, for a driver call that never returns at all.
_CONTROL_DEADLINE_S = 20


def _manager(profile: Path, proxy_url: str | None) -> BrowserManager:
    """The product's launch, with the proxy set through its own setting."""
    config = BrowserConfig(proxy_server=proxy_url)
    config.validate()
    launch_options, viewport = build_launch_options(config)
    return BrowserManager(
        user_data_dir=profile, headless=True, viewport=viewport, **launch_options
    )


async def _resolved_browser(probe: BrowserManager) -> str | None:
    playwright = await async_playwright().start()
    try:
        probe._playwright = playwright
        return probe._executable_about_to_run()
    finally:
        probe._playwright = None
        await playwright.stop()


def _common_name(pem: Path) -> str:
    certificate = x509.load_pem_x509_certificate(pem.read_bytes())
    (attribute,) = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attribute.value)


def _fingerprint(pem: Path) -> str:
    certificate = x509.load_pem_x509_certificate(pem.read_bytes())
    return certificate.fingerprint(hashes.SHA256()).hex()


def _not_loopback(host: str) -> list[str]:
    """What the operating system resolves *host* to besides loopback."""
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        return [f"unresolved ({error})"]
    return sorted(
        {
            str(info[4][0])
            for info in infos
            if not ipaddress.ip_address(info[4][0]).is_loopback
        }
    )


class _CanaryHandler(BaseHTTPRequestHandler):
    server: _Canary

    def do_GET(self) -> None:
        self.server.hosts.append(self.headers.get("Host", ""))
        # 200 rather than 204, which aborts the navigation that asked for it.
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        pass


class _Canary(ThreadingHTTPServer):
    # Threaded, because a browser may open a connection it never sends on,
    # and on a single thread that one socket would hold every later request
    # and the shutdown behind it.
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _CanaryHandler)
        self.hosts: list[str] = []


@contextmanager
def _canary() -> Iterator[_Canary]:
    server = _Canary()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def _close_under(manager: BrowserManager, error: BaseException) -> None:
    """Close after a failure, keeping that failure first but not the only word."""
    if not await manager.close():
        error.add_note("The browser's shutdown was not confirmed either.")


async def _unproxied_browser_honours_the_hosts_file(tmp_path: Path) -> None:
    with _canary() as canary:
        port = canary.server_address[1]
        manager = _manager(tmp_path / "canary-profile", None)
        await manager.start()
        try:
            try:
                await manager.page.goto(
                    f"http://{CANARY_HOST}:{port}/",
                    timeout=_NAVIGATION_TIMEOUT_MS,
                )
            except PlaywrightError as error:
                pytest.fail(
                    f"an unproxied browser did not reach {CANARY_HOST} on "
                    f"loopback ({error}), so its resolver may not read the "
                    f"hosts file; stopping before www.linkedin.com is named"
                )
        except BaseException as error:
            await _close_under(manager, error)
            raise
        assert await manager.close(), "the canary browser's shutdown was not confirmed"
        assert f"{CANARY_HOST}:{port}" in canary.hosts, canary.hosts


async def test_the_bundled_browser_loads_a_synthetic_origin_through_the_proxy(
    tmp_path, certificates, synthetic_egress
):
    origin, proxy = synthetic_egress
    # Read back rather than assumed: the issuer the browser reports below has
    # to be the CA this run issued and the CI step trusted.
    assert _common_name(certificates / CA_FILE) == CA_COMMON_NAME

    # Before any browser exists: were the real names fenced off?
    unfenced = {
        host: addresses
        for host in (*ALLOWED_HOSTS, CANARY_HOST)
        if (addresses := _not_loopback(host))
    }
    if unfenced:
        pytest.fail(
            f"the hosts file does not send these names to loopback only: "
            f"{unfenced}; stopping before a browser starts"
        )
    await _unproxied_browser_honours_the_hosts_file(tmp_path)

    stranger_certificates = tmp_path / "stranger"
    issue_certificates(
        stranger_certificates, ca_common_name="linkedin-mcp untrusted stranger CA"
    )
    stranger = SyntheticOrigin(stranger_certificates)
    stranger.start()
    try:
        browser_version = await _measure(tmp_path, origin, proxy, stranger)
    finally:
        stranger.stop()

    forwarded = proxy.forwarded()
    assert any(
        decision.host == "www.linkedin.com" and decision.port == 443
        for decision in forwarded
    ), proxy.decisions
    left_for_elsewhere = [
        decision for decision in forwarded if decision.host not in ALLOWED_HOSTS
    ]
    assert not left_for_elsewhere, left_for_elsewhere
    # Evidence for the run log, not an assertion: what the browser tried to
    # reach on its own and the proxy turned away.
    print(
        f"synthetic origin: browser {browser_version}; CA sha256 "
        f"{_fingerprint(certificates / CA_FILE)}; forwarded "
        f"{sorted({(d.host, d.port) for d in forwarded})}; refused "
        f"{sorted({d.target for d in proxy.refused()})}"
    )


async def _measure(
    tmp_path: Path,
    origin: SyntheticOrigin,
    proxy: EgressProxy,
    stranger: SyntheticOrigin,
) -> str:
    """Run the three proxied requests and return the browser's version."""
    # Settled before the launch, so everything after it fails rather than
    # skips: opting in is the promise that a browser is installed.
    executable = await _resolved_browser(
        _manager(tmp_path / "probe-profile", proxy.url)
    )
    assert executable is not None and Path(executable).exists(), (
        f"no browser installed at {executable}; the CI step installs one first"
    )

    manager = _manager(tmp_path / "profile", proxy.url)
    await manager.start()
    try:
        page = manager.page
        browser = manager.context.browser
        browser_version = browser.version if browser is not None else "unknown"

        try:
            control = await asyncio.wait_for(
                page.evaluate(
                    "timeout => fetch('https://refused.invalid/', "
                    "{signal: AbortSignal.timeout(timeout)})"
                    ".then(() => 'loaded', String)",
                    _CONTROL_TIMEOUT_MS,
                ),
                _CONTROL_DEADLINE_S,
            )
        except TimeoutError:
            pytest.fail(
                f"the control request did not settle within "
                f"{_CONTROL_DEADLINE_S}s; stopping before www.linkedin.com is "
                f"named. Proxy log: {proxy.decisions}"
            )
        assert control != "loaded", "a refused name loaded"
        assert any(
            decision.host == "refused.invalid" and not decision.forwarded
            for decision in proxy.refused()
        ), (
            "the proxy did not refuse the control request, so either the "
            "browser is not routing through it or it is not failing closed; "
            "stopping before www.linkedin.com could reach the real site. "
            f"Proxy log: {proxy.decisions}"
        )

        try:
            response = await page.goto(
                "https://www.linkedin.com/feed/", timeout=_NAVIGATION_TIMEOUT_MS
            )
        except PlaywrightError as error:
            pytest.fail(
                f"the synthetic feed did not load: {error}. Origin-side "
                f"connection failures: {origin.failures}. Proxy log: "
                f"{proxy.decisions}"
            )
        assert response is not None and response.status == 200
        assert FEED_MARKER in await page.content()
        details = await response.security_details()
        assert details is not None, "the feed arrived without TLS"
        assert details.get("subjectName") == "www.linkedin.com", details
        assert details.get("issuer") == CA_COMMON_NAME, details
        assert any(
            request.path == "/feed/"
            and request.host == "www.linkedin.com"
            and request.server_name == "www.linkedin.com"
            for request in origin.requests
        ), origin.requests

        proxy.route("static.licdn.com", stranger.port)
        with pytest.raises(PlaywrightError, match="ERR_CERT_AUTHORITY_INVALID"):
            await page.goto("https://static.licdn.com/", timeout=_NAVIGATION_TIMEOUT_MS)
        assert not stranger.requests, "a certificate nobody trusts was accepted"
    except BaseException as error:
        await _close_under(manager, error)
        raise
    assert await manager.close(), "the browser's shutdown was not confirmed"
    return browser_version
