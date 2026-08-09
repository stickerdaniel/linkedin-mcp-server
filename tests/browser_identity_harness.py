"""Two loopback origins that let a browser describe itself, in four realms.

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

**There are two servers, because one of the four surfaces is another origin.**
A port is part of an origin, so a second loopback listener is genuinely
cross-origin to the first, with no certificate and no hosts entry. The iframe
matters on its own: a user-agent override reaches the top document and misses
the frame, which is the same failure as the one it misses in a service worker.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import quote, urlsplit

#: Asked for on every response, so the *next* request from the same origin
#: carries the high-entropy client hints. They are absent from the first
#: request by design, which is why the page fetches ``/echo`` after loading
#: rather than reading the headers of its own document.
_ACCEPT_CH = "Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Full-Version-List"

#: Shared by the page and by the cross-origin frame, so the two document
#: realms are described the same way rather than one being asked less than
#: the other. A frame is a realm of its own: a patch applied only there is
#: invisible to everything the top document can read.
_ACCESSORS_JS = b"""
const describeAccessor = (name) => {
  const d = Object.getOwnPropertyDescriptor(Navigator.prototype, name);
  if (!d) return null;
  return {
    get: typeof d.get, set: typeof d.set,
    enumerable: d.enumerable, configurable: d.configurable,
    // The source, because that is what separates a native accessor from one
    // somebody redefined. The descriptor alone does not: redefining an
    // existing configurable property leaves every attribute the caller did
    // not name exactly as it was.
    //
    // Through Function.prototype.toString and never String(), which would
    // call the getter's *own* toString if it has one. Measured: a redefined
    // getter carrying `toString: () => 'function get webdriver() { [native
    // code] }'` produced exactly that text and passed for native.
    getSource: d.get ? Function.prototype.toString.call(d.get) : null,
  };
};

const describeAccessors = () => ({
  ownWebdriver: Object.hasOwn(navigator, 'webdriver'),
  // The reader itself, because everything else trusts it. A patched
  // Function.prototype.toString could otherwise say whatever it liked about
  // every accessor at once.
  toStringSource: Function.prototype.toString.call(Function.prototype.toString),
  webdriverDescriptor: describeAccessor('webdriver'),
  userAgentDescriptor: describeAccessor('userAgent'),
});
"""

_PAGE = b"""<!doctype html>
<title>identity</title>
<body><pre id="out"></pre>
<script>
// Deliberately not page.evaluate(): that runs in an isolated world. This is
// the realm a website actually gets.
const REALM_BUDGET_MS = __REALM_BUDGET_MS__;
__ACCESSORS_JS__

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
    platform: navigator.userAgentData ? navigator.userAgentData.platform : null,
    mobile: navigator.userAgentData ? navigator.userAgentData.mobile : null,
    hasWebdriver: 'webdriver' in navigator,
    webdriver: navigator.webdriver,
    accessors: describeAccessors(),
  });

  const askTheWorker = () => {
    const worker = new Worker('/worker.js');
    return new Promise((resolve, reject) => {
      worker.onmessage = e => resolve(e.data);
      worker.onerror = e => reject(new Error('worker: ' + e.message));
      setTimeout(() => reject(new Error('worker timed out')), REALM_BUDGET_MS);
    });
  };

  const askTheServiceWorker = async () => {
    const registration = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    return new Promise((resolve, reject) => {
      navigator.serviceWorker.addEventListener('message', e => resolve(e.data));
      setTimeout(() => reject(new Error('service worker timed out')), REALM_BUDGET_MS);
      (registration.active || navigator.serviceWorker.controller).postMessage('describe');
    });
  };

  // Another origin, because a port is part of one. The frame reports through
  // postMessage; nothing here can read across the boundary, which is the point.
  const askTheFrame = () => new Promise((resolve, reject) => {
    addEventListener('message', e => {
      if (e.data && e.data.realm === 'iframe') resolve(e.data);
    });
    setTimeout(() => reject(new Error('cross-origin iframe timed out')), REALM_BUDGET_MS);
    const el = document.createElement('iframe');
    el.src = new URLSearchParams(location.search).get('frame') + 'frame';
    document.body.appendChild(el);
  });

  // Concurrently, and that is a correctness point rather than a speed one.
  // Awaited one after another, three budgets of REALM_BUDGET_MS could add up
  // past the host's own timeout, so a slow-but-working browser would be
  // reported as a broken one. Started together, the whole page costs one
  // budget and the host's bound stays comfortably above it.
  const [dedicated, serviceWorker, iframe] = await Promise.all([
    askTheWorker(), askTheServiceWorker(), askTheFrame(),
  ]);

  const result = {
    page: {...(await jsChannel()), headers: await echo()},
    dedicated,
    serviceWorker,
    iframe,
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
      // `cdc_` with no dollar as well: ChromeDriver installs
      // `cdc_adoQpoasnfa76pfcZLmcfl_Array` and friends under the bare prefix,
      // and only the older `$cdc_` spelling was being looked for.
      .filter(n => /^(__pw|__playwright|\\$?cdc_|__driver|__selenium|__webdriver|__fxdriver|__nightmare|_Selenium_IDE|domAutomation)/.test(n)),
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
  postMessage({realm: 'dedicated', ua: navigator.userAgent,
    hasWebdriver: 'webdriver' in navigator,
    webdriver: navigator.webdriver, headers});
})();
"""

_SERVICE_WORKER = b"""
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
// waitUntil, not a bare async listener: an EventTarget ignores what a listener
// returns, so without it the browser is free to terminate the worker at the
// first await. Under load that is a real answer that never arrives, and the
// page then waits out its own timeout.
self.addEventListener('message', e => e.waitUntil((async () => {
  const headers = await (await fetch('/echo', {cache: 'no-store'})).json();
  e.source.postMessage({realm: 'serviceWorker', ua: navigator.userAgent,
    hasWebdriver: 'webdriver' in navigator,
    webdriver: navigator.webdriver, headers});
})()));
"""

_FRAME = b"""<!doctype html>
<title>frame</title>
<script>
__ACCESSORS_JS__
(async () => {
  const headers = await (await fetch('/echo', {cache: 'no-store'})).json();
  parent.postMessage({realm: 'iframe', ua: navigator.userAgent,
    hasWebdriver: 'webdriver' in navigator,
    webdriver: navigator.webdriver,
    accessors: describeAccessors(), headers}, '*');
})();
</script>
"""

#: How long any one realm may take to answer, and how much longer the host
#: waits for the whole page. The two are declared together on purpose: they
#: used to be written out separately, and three sequential realm budgets added
#: up past the host's bound, so a browser that was merely slow reported as a
#: browser that was broken. The realms run concurrently now, which is what
#: makes one budget the cost of the page rather than three.
REALM_BUDGET_MS = 15_000
HOST_BUDGET_MS = REALM_BUDGET_MS * 3

_PAGE = _PAGE.replace(b"__REALM_BUDGET_MS__", str(REALM_BUDGET_MS).encode())
_PAGE = _PAGE.replace(b"__ACCESSORS_JS__", _ACCESSORS_JS)
_FRAME = _FRAME.replace(b"__ACCESSORS_JS__", _ACCESSORS_JS)

_BODIES = {
    "/": (_PAGE, "text/html; charset=utf-8"),
    "/frame": (_FRAME, "text/html; charset=utf-8"),
    "/worker.js": (_WORKER, "text/javascript"),
    "/sw.js": (_SERVICE_WORKER, "text/javascript"),
}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        # The query carries the other origin, so the path has to be taken apart
        # rather than matched whole: `/?frame=http%3A%2F%2F...` is still `/`.
        path = urlsplit(self.path).path
        if path == "/echo":
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
            ctype = "application/json"
        else:
            body, ctype = _BODIES.get(path, (b"not found", "text/plain"))
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
    """Serves the identity page on two loopback ports picked by the OS.

    Two, because the fourth surface is a cross-origin frame and a port is part
    of an origin. Both listeners serve the same handler; which one is "the
    other origin" is decided only by which URL the page is given.
    """

    def __init__(self) -> None:
        self._servers = [
            ThreadingHTTPServer(("127.0.0.1", 0), _Handler) for _ in range(2)
        ]
        self._threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in self._servers
        ]

    def __enter__(self) -> "IdentityServer":
        for thread in self._threads:
            thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()

    def _origin(self, index: int) -> str:
        host, port = self._servers[index].server_address[:2]
        return f"http://{host}:{port}/"

    @property
    def url(self) -> str:
        """The page, told where to find the other origin.

        Passed in the query string rather than hardcoded, because the port is
        the OS's choice and the page has no other way to learn it.
        """
        return f"{self._origin(0)}?frame={quote(self._origin(1), safe='')}"


async def describe_browser(
    page: Any, url: str, timeout_ms: int = HOST_BUDGET_MS
) -> dict:
    """Navigate *page* at the harness and return what the browser said it is."""
    await page.goto(url, wait_until="domcontentloaded")
    await page.wait_for_function(
        "document.getElementById('out').textContent.length > 0", timeout=timeout_ms
    )
    described = json.loads(await page.eval_on_selector("#out", "e => e.textContent"))
    if "error" in described:
        raise RuntimeError(f"the identity page failed: {described['error']}")
    return described
