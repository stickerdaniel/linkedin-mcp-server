"""A loopback origin that lets a browser describe itself, in three realms.

Kept apart from the test so the same harness can run inside the container
image, which has no dev dependencies. Nothing here imports pytest, and the only
third-party import is the browser itself.

Two things about the design are not stylistic.

**The page collects its own values.** Everything is gathered by a ``<script>``
the page runs and published into the DOM; the test only reads the result.
Patchright evaluates ``page.evaluate()`` in an isolated world, which is not
what a website sees, so measuring through it would measure the wrong realm.

**The origin is loopback, over plain HTTP.** ``http://127.0.0.1`` is a secure
context by definition, so service workers and ``navigator.userAgentData`` are
available without a certificate. A self-signed certificate plus
``ignore_https_errors`` would perturb the very thing being measured.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

#: Asked for on every response, so the *next* request from the same origin
#: carries the high-entropy client hints. They are absent from the first
#: request by design, which is why the page fetches ``/echo`` after loading
#: rather than reading the headers of its own document.
_ACCEPT_CH = "Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Full-Version-List"

_PAGE = b"""<!doctype html>
<title>identity</title>
<body><pre id="out"></pre>
<script>
// Deliberately not page.evaluate(): that runs in an isolated world. This is
// the realm a website actually gets.
(async () => {
  const echo = async () => (await fetch('/echo', {cache: 'no-store'})).json();
  const jsChannel = async () => ({
    ua: navigator.userAgent,
    brands: navigator.userAgentData
      ? navigator.userAgentData.brands.map(b => ({brand: b.brand, version: b.version}))
      : null,
    highEntropy: navigator.userAgentData
      ? await navigator.userAgentData.getHighEntropyValues(
          ['architecture', 'bitness', 'fullVersionList'])
      : null,
    webdriver: navigator.webdriver,
  });

  const worker = new Worker('/worker.js');
  const dedicated = await new Promise((resolve, reject) => {
    worker.onmessage = e => resolve(e.data);
    worker.onerror = e => reject(new Error('worker: ' + e.message));
    setTimeout(() => reject(new Error('worker timed out')), 15000);
  });

  const registration = await navigator.serviceWorker.register('/sw.js');
  await navigator.serviceWorker.ready;
  const serviceWorker = await new Promise((resolve, reject) => {
    navigator.serviceWorker.addEventListener('message', e => resolve(e.data));
    setTimeout(() => reject(new Error('service worker timed out')), 15000);
    (registration.active || navigator.serviceWorker.controller).postMessage('describe');
  });

  const result = {
    page: {...(await jsChannel()), headers: await echo()},
    dedicated,
    serviceWorker,
    // Read here rather than in the assertions: outerWidth is a property of the
    // window the page is in, and there is no other realm that can see it.
    geometry: {
      outerWidth: outerWidth, outerHeight: outerHeight,
      screenWidth: screen.width, screenHeight: screen.height,
    },
    plugins: navigator.plugins.length,
    hasChromeObject: typeof window.chrome,
    // Automation artefacts a launcher can leave in the page's own realm.
    automationGlobals: Object.getOwnPropertyNames(window)
      .filter(n => /^(__pw|__playwright|\\$cdc_|__driver|__selenium|__webdriver)/.test(n)),
    webdriverDescriptor: (() => {
      const d = Object.getOwnPropertyDescriptor(Navigator.prototype, 'webdriver');
      return d ? {get: typeof d.get, set: typeof d.set, configurable: d.configurable} : null;
    })(),
  };
  document.getElementById('out').textContent = JSON.stringify(result);
})().catch(err => {
  document.getElementById('out').textContent = JSON.stringify({error: String(err)});
});
</script>
"""

_WORKER = b"""
(async () => {
  const headers = await (await fetch('/echo', {cache: 'no-store'})).json();
  postMessage({realm: 'dedicated', ua: navigator.userAgent, headers});
})();
"""

_SERVICE_WORKER = b"""
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('message', async e => {
  const headers = await (await fetch('/echo', {cache: 'no-store'})).json();
  e.source.postMessage({realm: 'serviceWorker', ua: navigator.userAgent, headers});
});
"""

_BODIES = {
    "/": (_PAGE, "text/html; charset=utf-8"),
    "/worker.js": (_WORKER, "text/javascript"),
    "/sw.js": (_SERVICE_WORKER, "text/javascript"),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        if self.path.startswith("/echo"):
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            ctype = "application/json"
        else:
            body, ctype = _BODIES.get(self.path, (b"not found", "text/plain"))
        self.send_response(200 if body != b"not found" else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-CH", _ACCEPT_CH)
        # A service worker may only claim a scope at or below its own path
        # unless the server says otherwise.
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silent. The default writes every request to stderr."""


class IdentityServer:
    """Serves the identity page on a loopback port picked by the OS."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "IdentityServer":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/"


async def describe_browser(page: Any, url: str, timeout_ms: int = 30_000) -> dict:
    """Navigate *page* at the harness and return what the browser said it is."""
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "document.getElementById('out').textContent.length > 0", timeout=timeout_ms
    )
    described = json.loads(await page.eval_on_selector("#out", "e => e.textContent"))
    if "error" in described:
        raise RuntimeError(f"the identity page failed: {described['error']}")
    return described
