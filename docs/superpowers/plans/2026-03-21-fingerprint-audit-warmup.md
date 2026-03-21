# Fingerprint Audit + Warm-up Robustness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a diagnostic tool to compare Playwright vs real Chrome fingerprints, and make the warm-up robust enough to gate tool calls until complete.

**Architecture:** Two independent deliverables: (1) a standalone fingerprint audit tool (`tools/`) with a CLI flag (`--fingerprint-audit`), and (2) warm-up improvements in `core/auth.py` with a completion gate in `drivers/browser.py`. The audit tool is self-contained — no production code depends on it.

**Tech Stack:** Python stdlib `http.server` (diagnostic server), Patchright (browser), pytest + unittest.mock (tests)

---

### Task 1: Fingerprint Collection HTML Page

**Files:**
- Create: `tools/fingerprint_page.html`

- [ ] **Step 1: Create the fingerprint collection HTML page**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Fingerprint Audit</title>
  <style>
    body { font-family: monospace; max-width: 800px; margin: 40px auto; padding: 0 20px; }
    pre { background: #f4f4f4; padding: 16px; overflow-x: auto; }
    .status { font-size: 1.2em; margin: 20px 0; }
  </style>
</head>
<body>
  <h1>Browser Fingerprint Audit</h1>
  <div class="status" id="status">Collecting fingerprint...</div>
  <pre id="result"></pre>
  <script>
    (async function() {
      const fp = {};

      // Navigator properties
      fp.webdriver = navigator.webdriver;
      fp.userAgent = navigator.userAgent;
      fp.platform = navigator.platform;
      fp.languages = navigator.languages ? [...navigator.languages] : null;
      fp.language = navigator.language;
      fp.hardwareConcurrency = navigator.hardwareConcurrency;
      fp.deviceMemory = navigator.deviceMemory || null;
      fp.maxTouchPoints = navigator.maxTouchPoints;
      fp.cookieEnabled = navigator.cookieEnabled;
      fp.doNotTrack = navigator.doNotTrack;
      fp.pdfViewerEnabled = navigator.pdfViewerEnabled;

      // Plugins
      fp.pluginCount = navigator.plugins.length;
      fp.plugins = [];
      for (let i = 0; i < navigator.plugins.length; i++) {
        fp.plugins.push({
          name: navigator.plugins[i].name,
          filename: navigator.plugins[i].filename,
          description: navigator.plugins[i].description,
        });
      }

      // Screen
      fp.screen = {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth,
      };
      fp.innerWidth = window.innerWidth;
      fp.innerHeight = window.innerHeight;
      fp.outerWidth = window.outerWidth;
      fp.outerHeight = window.outerHeight;
      fp.devicePixelRatio = window.devicePixelRatio;

      // window.chrome
      fp.hasWindowChrome = typeof window.chrome !== 'undefined';
      fp.windowChromeKeys = typeof window.chrome === 'object' && window.chrome !== null
        ? Object.keys(window.chrome) : null;

      // Performance.memory
      fp.hasPerformanceMemory = typeof performance.memory !== 'undefined';
      fp.performanceMemory = performance.memory ? {
        jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
        totalJSHeapSize: performance.memory.totalJSHeapSize,
        usedJSHeapSize: performance.memory.usedJSHeapSize,
      } : null;

      // Notification
      fp.notificationPermission = typeof Notification !== 'undefined'
        ? Notification.permission : null;

      // WebGL
      try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (gl) {
          const ext = gl.getExtension('WEBGL_debug_renderer_info');
          fp.webgl = {
            vendor: gl.getParameter(gl.VENDOR),
            renderer: gl.getParameter(gl.RENDERER),
            unmaskedVendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
            unmaskedRenderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
          };
        } else {
          fp.webgl = null;
        }
      } catch (e) {
        fp.webgl = { error: e.message };
      }

      // Canvas fingerprint
      try {
        const canvas = document.createElement('canvas');
        canvas.width = 200;
        canvas.height = 50;
        const ctx = canvas.getContext('2d');
        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(125, 1, 62, 20);
        ctx.fillStyle = '#069';
        ctx.fillText('Fingerprint', 2, 15);
        ctx.fillStyle = 'rgba(102, 204, 0, 0.7)';
        ctx.fillText('Fingerprint', 4, 17);
        fp.canvasHash = canvas.toDataURL().length;  // hash by length for simplicity
      } catch (e) {
        fp.canvasHash = null;
      }

      // Timezone
      fp.timezoneOffset = new Date().getTimezoneOffset();
      fp.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

      // Connection
      fp.connection = navigator.connection ? {
        effectiveType: navigator.connection.effectiveType,
        downlink: navigator.connection.downlink,
        rtt: navigator.connection.rtt,
      } : null;

      // Post to server
      document.getElementById('result').textContent = JSON.stringify(fp, null, 2);
      try {
        const resp = await fetch('/collect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(fp),
        });
        const data = await resp.json();
        document.getElementById('status').textContent =
          'Fingerprint collected and saved as: ' + data.source;
      } catch (e) {
        document.getElementById('status').textContent =
          'Fingerprint collected (could not POST to server: ' + e.message + ')';
      }
    })();
  </script>
</body>
</html>
```

- [ ] **Step 2: Verify the file was created**

Run: `ls -la tools/fingerprint_page.html`
Expected: file exists

- [ ] **Step 3: Commit**

```bash
git add tools/fingerprint_page.html
git commit -m "feat(audit): add fingerprint collection HTML page"
```

---

### Task 2: Fingerprint Diagnostic Server

**Files:**
- Create: `tools/fingerprint_server.py`

- [ ] **Step 1: Create the diagnostic HTTP server**

```python
#!/usr/bin/env python3
"""Fingerprint audit diagnostic server.

Serves an HTML page that collects browser fingerprint data and saves results
as JSON for comparison between Playwright and real Chrome.

Usage:
    python tools/fingerprint_server.py

Then navigate to http://localhost:8765 in a real Chrome browser to capture
the baseline, and run `uv run -m linkedin_mcp_server --fingerprint-audit`
to capture the Playwright fingerprint.
"""

import json
import sys
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

RESULTS_DIR = Path(__file__).parent / "fingerprint_results"
HTML_PATH = Path(__file__).parent / "fingerprint_page.html"
PORT = 8765

# Track which sources have reported
_received: dict[str, dict] = {}
_received_lock = threading.Lock()


class FingerprintHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/status":
            self._serve_status()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        if self.path != "/collect":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            fingerprint = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
            return

        # Determine source from query param or header
        source = "unknown"
        if "?source=playwright" in self.path or self.headers.get("X-Source") == "playwright":
            source = "playwright"
        elif "?source=baseline" in self.path or self.headers.get("X-Source") == "baseline":
            source = "baseline"
        else:
            # Auto-detect: first POST is baseline, second is playwright
            with _received_lock:
                if "baseline" not in _received:
                    source = "baseline"
                elif "playwright" not in _received:
                    source = "playwright"

        # Add server-side data
        result = {
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "client_fingerprint": fingerprint,
            "http_headers": dict(self.headers),
            "remote_addr": self.client_address[0],
        }

        # Save to file
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        filename = f"{timestamp}-{source}.json"
        filepath = RESULTS_DIR / filename
        filepath.write_text(json.dumps(result, indent=2))

        with _received_lock:
            _received[source] = result

        print(f"\n[{source.upper()}] Fingerprint received and saved to {filepath}")

        # If both collected, generate diff
        with _received_lock:
            if "baseline" in _received and "playwright" in _received:
                self._generate_diff()

        # Respond
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "source": source}).encode())

    def _serve_html(self):
        try:
            html = HTML_PATH.read_text()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND, "fingerprint_page.html not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_status(self):
        with _received_lock:
            status = {
                "baseline": "baseline" in _received,
                "playwright": "playwright" in _received,
            }
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(status).encode())

    def _generate_diff(self):
        baseline = _received["baseline"]
        playwright = _received["playwright"]

        print("\n" + "=" * 70)
        print("FINGERPRINT DIFF: Baseline (Chrome) vs Playwright")
        print("=" * 70)

        # Compare HTTP headers
        print("\n--- HTTP Headers ---")
        b_headers = baseline["http_headers"]
        p_headers = playwright["http_headers"]
        all_header_keys = sorted(set(list(b_headers.keys()) + list(p_headers.keys())))
        header_diffs = 0
        for key in all_header_keys:
            b_val = b_headers.get(key, "<missing>")
            p_val = p_headers.get(key, "<missing>")
            if b_val != p_val:
                header_diffs += 1
                print(f"  {key}:")
                print(f"    Chrome:     {b_val}")
                print(f"    Playwright: {p_val}")

        if header_diffs == 0:
            print("  (no differences)")

        # Compare client fingerprints
        print("\n--- Client Fingerprint ---")
        b_fp = baseline["client_fingerprint"]
        p_fp = playwright["client_fingerprint"]
        all_fp_keys = sorted(set(list(b_fp.keys()) + list(p_fp.keys())))
        fp_diffs = 0
        for key in all_fp_keys:
            b_val = b_fp.get(key, "<missing>")
            p_val = p_fp.get(key, "<missing>")
            if b_val != p_val:
                fp_diffs += 1
                print(f"  {key}:")
                print(f"    Chrome:     {json.dumps(b_val)}")
                print(f"    Playwright: {json.dumps(p_val)}")

        if fp_diffs == 0:
            print("  (no differences)")

        print(f"\nTotal: {header_diffs} header diff(s), {fp_diffs} fingerprint diff(s)")
        print("=" * 70)

        # Save diff to file
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        diff_path = RESULTS_DIR / f"{timestamp}-diff.txt"
        # Redirect diff to file too
        import io
        buf = io.StringIO()
        buf.write("FINGERPRINT DIFF: Baseline (Chrome) vs Playwright\n")
        buf.write("=" * 70 + "\n")
        buf.write(f"\nHTTP Header diffs: {header_diffs}\n")
        for key in all_header_keys:
            b_val = b_headers.get(key, "<missing>")
            p_val = p_headers.get(key, "<missing>")
            if b_val != p_val:
                buf.write(f"  {key}: Chrome={b_val!r} | Playwright={p_val!r}\n")
        buf.write(f"\nClient fingerprint diffs: {fp_diffs}\n")
        for key in all_fp_keys:
            b_val = b_fp.get(key, "<missing>")
            p_val = p_fp.get(key, "<missing>")
            if b_val != p_val:
                buf.write(f"  {key}: Chrome={json.dumps(b_val)} | Playwright={json.dumps(p_val)}\n")
        diff_path.write_text(buf.getvalue())
        print(f"\nDiff saved to {diff_path}")

    def log_message(self, format, *args):
        """Suppress default access log noise."""
        pass


def run_server(port: int = PORT) -> HTTPServer:
    """Start the fingerprint audit server."""
    server = HTTPServer(("0.0.0.0", port), FingerprintHandler)
    print(f"Fingerprint audit server running on http://localhost:{port}")
    print(f"Results will be saved to {RESULTS_DIR}/")
    print()
    print("Step 1: Open http://localhost:{} in real Chrome to capture baseline".format(port))
    print("Step 2: Run `uv run -m linkedin_mcp_server --fingerprint-audit` for Playwright")
    print()
    return server


if __name__ == "__main__":
    server = run_server()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        sys.exit(0)
```

- [ ] **Step 2: Verify the file was created and is executable**

Run: `python -c "import py_compile; py_compile.compile('tools/fingerprint_server.py', doraise=True)" && echo OK`
Expected: OK (compiles without syntax errors)

- [ ] **Step 3: Commit**

```bash
git add tools/fingerprint_server.py
git commit -m "feat(audit): add fingerprint diagnostic HTTP server"
```

---

### Task 3: CLI `--fingerprint-audit` Flag

**Files:**
- Modify: `linkedin_mcp_server/config/schema.py:62` (add `fingerprint_audit` field to `ServerConfig`)
- Modify: `linkedin_mcp_server/config/loaders.py:168-340` (add `--fingerprint-audit` arg)
- Modify: `linkedin_mcp_server/cli_main.py:305-403` (handle `--fingerprint-audit` in `main()`)
- Test: `tests/test_config.py` (add test for new flag)
- Test: `tests/test_cli_main.py` (add test for audit flow)

- [ ] **Step 1: Write failing test for config schema**

In `tests/test_config.py`, add:

```python
def test_server_config_fingerprint_audit_default():
    from linkedin_mcp_server.config.schema import ServerConfig
    config = ServerConfig()
    assert config.fingerprint_audit is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_server_config_fingerprint_audit_default -v`
Expected: FAIL with AttributeError

- [ ] **Step 3: Add `fingerprint_audit` to ServerConfig**

In `linkedin_mcp_server/config/schema.py`, add to the `ServerConfig` dataclass after `status`:

```python
fingerprint_audit: bool = False  # Run fingerprint audit and exit
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py::test_server_config_fingerprint_audit_default -v`
Expected: PASS

- [ ] **Step 5: Add `--fingerprint-audit` to argparse**

In `linkedin_mcp_server/config/loaders.py`, after the `--logout` argument block (around line 271), add:

```python
parser.add_argument(
    "--fingerprint-audit",
    action="store_true",
    help="Run fingerprint audit: compare Playwright vs real Chrome fingerprints",
)
```

And in the argument application section (around line 335), add:

```python
if args.fingerprint_audit:
    config.server.fingerprint_audit = True
```

- [ ] **Step 6: Add audit handler function in cli_main.py**

In `linkedin_mcp_server/cli_main.py`, add a new function before `main()`:

```python
def fingerprint_audit_and_exit() -> None:
    """Run fingerprint audit: launch diagnostic server, then Playwright, compare results."""
    import subprocess
    import sys
    import time
    import json
    from pathlib import Path
    from urllib.request import urlopen, Request
    from urllib.error import URLError

    config = get_config()
    configure_logging(log_level="INFO", json_format=False)

    version = get_version()
    logger.info(f"LinkedIn MCP Server v{version} - Fingerprint Audit mode")

    tools_dir = Path(__file__).parent.parent / "tools"
    server_script = tools_dir / "fingerprint_server.py"

    if not server_script.exists():
        print(f"Error: {server_script} not found")
        print("Make sure you're running from the project root directory")
        sys.exit(1)

    # Start the diagnostic server in a subprocess
    server_proc = subprocess.Popen(
        [sys.executable, str(server_script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    port = 8765
    url = f"http://localhost:{port}"

    # Wait for server to be ready
    for _ in range(20):
        try:
            urlopen(f"{url}/status", timeout=1)
            break
        except (URLError, OSError):
            time.sleep(0.25)
    else:
        print("Error: Diagnostic server failed to start")
        server_proc.terminate()
        sys.exit(1)

    print(f"\nFingerprint audit server running on {url}")
    print(f"\nStep 1: Open {url} in real Chrome to capture baseline fingerprint")
    print("        (waiting for baseline...)")

    # Wait for baseline
    while True:
        try:
            resp = urlopen(f"{url}/status", timeout=2)
            status = json.loads(resp.read())
            if status.get("baseline"):
                print("Baseline captured!")
                break
        except (URLError, OSError):
            pass
        time.sleep(1)

    print("\nStep 2: Launching Playwright to capture automated fingerprint...")

    # Launch Playwright with production config
    import asyncio

    async def capture_playwright_fingerprint():
        from linkedin_mcp_server.core.browser import BrowserManager

        browser_config = config.browser
        async with BrowserManager(
            user_data_dir=Path.home() / ".linkedin-mcp" / "audit-profile",
            headless=browser_config.headless,
            channel=browser_config.channel,
            viewport={
                "width": browser_config.viewport_width,
                "height": browser_config.viewport_height,
            },
            locale=browser_config.locale,
            timezone_id=browser_config.timezone_id,
            accept_language=browser_config.accept_language,
        ) as browser:
            page = browser.page
            # Add source header so server knows it's Playwright
            await page.set_extra_http_headers({"X-Source": "playwright"})
            await page.goto(f"{url}/", wait_until="networkidle", timeout=15000)
            # Wait for the POST to complete
            await page.wait_for_function(
                "document.getElementById('status').textContent.includes('collected')",
                timeout=10000,
            )
            print("Playwright fingerprint captured!")

    try:
        asyncio.run(capture_playwright_fingerprint())
    except Exception as e:
        print(f"Error capturing Playwright fingerprint: {e}")
        server_proc.terminate()
        sys.exit(1)

    # Give server a moment to process and print diff
    time.sleep(1)

    # Clean up
    server_proc.terminate()
    server_proc.wait(timeout=5)

    # Clean up audit profile
    import shutil
    audit_profile = Path.home() / ".linkedin-mcp" / "audit-profile"
    if audit_profile.exists():
        shutil.rmtree(audit_profile, ignore_errors=True)

    print("\nAudit complete. Check the diff above and results in tools/fingerprint_results/")
    sys.exit(0)
```

- [ ] **Step 7: Wire the flag into main()**

In `linkedin_mcp_server/cli_main.py`, in the `main()` function, after the `--status` handling block (around line 338), add:

```python
# Handle --fingerprint-audit flag
if config.server.fingerprint_audit:
    fingerprint_audit_and_exit()
```

- [ ] **Step 8: Write test for the audit CLI path**

In `tests/test_cli_main.py`, add a test that verifies the fingerprint-audit flag is recognized and calls the handler:

```python
@patch("linkedin_mcp_server.cli_main.fingerprint_audit_and_exit")
@patch("linkedin_mcp_server.cli_main.get_config")
def test_fingerprint_audit_flag_calls_handler(mock_config, mock_audit):
    """--fingerprint-audit calls fingerprint_audit_and_exit()."""
    mock_audit.side_effect = SystemExit(0)
    config = MagicMock()
    config.server.fingerprint_audit = True
    config.server.logout = False
    config.server.login = False
    config.server.status = False
    config.server.log_level = "WARNING"
    config.is_interactive = False
    mock_config.return_value = config

    with pytest.raises(SystemExit):
        from linkedin_mcp_server.cli_main import main
        main()

    mock_audit.assert_called_once()
```

- [ ] **Step 9: Run all tests**

Run: `uv run pytest tests/test_config.py tests/test_cli_main.py -v`
Expected: all PASS

- [ ] **Step 10: Commit**

```bash
git add linkedin_mcp_server/config/schema.py linkedin_mcp_server/config/loaders.py linkedin_mcp_server/cli_main.py tests/test_config.py tests/test_cli_main.py
git commit -m "feat(audit): add --fingerprint-audit CLI flag with Playwright comparison"
```

---

### Task 4: Robust Warm-up

**Files:**
- Modify: `linkedin_mcp_server/core/auth.py:37-100` (rewrite `warm_up_browser`)
- Test: `tests/test_core_auth.py` (add warm-up tests)

- [ ] **Step 1: Write failing tests for warm-up behavior**

In `tests/test_core_auth.py`, add:

```python
@pytest.mark.asyncio
async def test_warm_up_browser_only_visits_external_sites():
    """warm_up_browser should NOT visit LinkedIn pages."""
    visited_urls = []

    page = MagicMock()

    async def mock_goto(url, **kwargs):
        visited_urls.append(url)

    page.goto = AsyncMock(side_effect=mock_goto)
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])

    from linkedin_mcp_server.core.auth import warm_up_browser
    await warm_up_browser(page)

    for url in visited_urls:
        assert "linkedin.com" not in url, f"Warm-up should not visit LinkedIn: {url}"
    assert len(visited_urls) >= 2, "Should visit at least 2 external sites"


@pytest.mark.asyncio
async def test_warm_up_browser_scrolls_external_pages():
    """warm_up_browser should scroll on external pages."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])

    from linkedin_mcp_server.core.auth import warm_up_browser
    await warm_up_browser(page)

    # mouse.wheel should have been called for scrolling
    assert page.mouse.wheel.call_count >= 3, "Should scroll on external pages"


@pytest.mark.asyncio
async def test_warm_up_browser_returns_success_status():
    """warm_up_browser should return a WarmUpResult with sites_visited count."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])

    from linkedin_mcp_server.core.auth import warm_up_browser
    result = await warm_up_browser(page)

    assert result.sites_visited > 0
    assert result.total_sites > 0
    assert result.elapsed_seconds > 0


@pytest.mark.asyncio
async def test_warm_up_browser_handles_all_failures():
    """warm_up_browser should return gracefully when all sites fail."""
    page = MagicMock()
    page.goto = AsyncMock(side_effect=Exception("network error"))
    page.viewport_size = {"width": 1280, "height": 720}
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    page.query_selector_all = AsyncMock(return_value=[])

    from linkedin_mcp_server.core.auth import warm_up_browser
    result = await warm_up_browser(page)

    assert result.sites_visited == 0
    assert result.success is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_core_auth.py::test_warm_up_browser_only_visits_external_sites tests/test_core_auth.py::test_warm_up_browser_returns_success_status -v`
Expected: FAIL (LinkedIn URLs present, no WarmUpResult)

- [ ] **Step 3: Rewrite warm_up_browser**

Replace the entire `warm_up_browser` function in `linkedin_mcp_server/core/auth.py:37-100` with:

```python
from dataclasses import dataclass
import time

@dataclass
class WarmUpResult:
    """Result of browser warm-up."""
    sites_visited: int
    total_sites: int
    elapsed_seconds: float

    @property
    def success(self) -> bool:
        return self.sites_visited > 0


async def warm_up_browser(page: Page) -> WarmUpResult:
    """Visit external sites to build a human-like browsing history before LinkedIn.

    Only visits non-LinkedIn sites (Google, Wikipedia, GitHub) with realistic
    dwell times, scrolling, and mouse movements. LinkedIn is intentionally excluded
    — the first LinkedIn contact should be the natural authenticated navigation.

    Returns a WarmUpResult with visit statistics.
    """
    from .stealth import hover_random_links, random_mouse_move

    external_sites = [
        "https://www.google.com",
        "https://www.wikipedia.org",
        "https://www.github.com",
    ]

    logger.info("Warming up browser (external sites only)...")
    start = time.monotonic()

    failures = 0
    total = len(external_sites)
    for site in external_sites:
        try:
            # Navigate with networkidle for full resource loading
            try:
                await page.goto(site, wait_until="networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                await page.goto(site, wait_until="domcontentloaded", timeout=10000)

            # Random mouse movements (3-5)
            await random_mouse_move(page, count=random.randint(3, 5))

            # Scroll like a real user (3-6 times)
            for _ in range(random.randint(3, 6)):
                await page.mouse.wheel(0, random.randint(300, 700))
                await asyncio.sleep(random.uniform(1.0, 3.0))

            # Hover over random links
            await hover_random_links(page, max_links=random.randint(1, 3))

            # Dwell on page (5-12s)
            await asyncio.sleep(random.uniform(5.0, 12.0))

            logger.debug("Warm-up visited %s", site)
        except Exception as e:
            failures += 1
            logger.debug("Warm-up: could not visit %s: %s", site, e)
            continue

    elapsed = time.monotonic() - start
    visited = total - failures
    result = WarmUpResult(
        sites_visited=visited,
        total_sites=total,
        elapsed_seconds=round(elapsed, 1),
    )

    if visited == 0:
        logger.warning("Warm-up failed: none of %d sites reachable", total)
    else:
        logger.info(
            "Warm-up complete: %d/%d external sites visited in %.0fs",
            visited, total, elapsed,
        )

    return result
```

Note: the `import time` and `WarmUpResult` dataclass should be at module level. Place `WarmUpResult` above the function, and add `import time` to the top-level imports.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_core_auth.py -v -k warm_up`
Expected: all 4 warm-up tests PASS

- [ ] **Step 5: Update ALL callers of warm_up_browser**

The return type changed from `None` to `WarmUpResult`. Two callers must be updated:

1. `linkedin_mcp_server/drivers/browser.py:302` — in `_bridge_runtime_profile`, capture the result:
   ```python
   warmup_result = await warm_up_browser(browser.page)
   logger.info("Bridge warm-up: %d/%d sites in %.0fs", warmup_result.sites_visited, warmup_result.total_sites, warmup_result.elapsed_seconds)
   ```

2. `linkedin_mcp_server/setup.py:57` — in `interactive_login`, the result can be ignored (login flow doesn't need it):
   ```python
   await warm_up_browser(browser.page)  # result intentionally unused in login flow
   ```

3. Update `tests/test_setup.py:47,83` — the existing `monkeypatch.setattr("linkedin_mcp_server.setup.warm_up_browser", AsyncMock())` returns `None` which is no longer the correct type. Change to:
   ```python
   from linkedin_mcp_server.core.auth import WarmUpResult
   monkeypatch.setattr("linkedin_mcp_server.setup.warm_up_browser", AsyncMock(return_value=WarmUpResult(sites_visited=3, total_sites=3, elapsed_seconds=1.0)))
   ```

4. Update `tests/test_browser_driver.py:1015,1053` — same fix for the mock return value.

Run: `uv run ruff check . && uv run ty check`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add linkedin_mcp_server/core/auth.py tests/test_core_auth.py
git commit -m "feat(warmup): remove LinkedIn from warm-up, increase dwell time, return WarmUpResult"
```

---

### Task 5: Warm-up Completion Gate

**Files:**
- Modify: `linkedin_mcp_server/drivers/browser.py` (add warm-up gate)
- Modify: `linkedin_mcp_server/dependencies.py` (check gate before tool calls)
- Test: `tests/test_dependencies.py` (test gate behavior)

- [ ] **Step 1: Write failing tests for warm-up gate**

In `tests/test_browser_driver.py`, add unit tests for the gate primitive itself:

```python
import asyncio
import pytest
from linkedin_mcp_server.drivers.browser import (
    ensure_warmup_complete,
    mark_warmup_complete,
    reset_warmup_gate,
)


@pytest.mark.asyncio
async def test_ensure_warmup_complete_returns_immediately_when_set():
    """Gate returns immediately if warm-up already complete."""
    mark_warmup_complete()
    await asyncio.wait_for(ensure_warmup_complete(), timeout=0.1)
    reset_warmup_gate()  # cleanup


@pytest.mark.asyncio
async def test_ensure_warmup_complete_blocks_until_set():
    """Gate blocks until mark_warmup_complete is called."""
    reset_warmup_gate()
    completed = False

    async def wait_then_mark():
        await asyncio.sleep(0.05)
        mark_warmup_complete()

    asyncio.create_task(wait_then_mark())
    await asyncio.wait_for(ensure_warmup_complete(), timeout=1.0)
    completed = True
    assert completed
    reset_warmup_gate()  # cleanup


@pytest.mark.asyncio
async def test_ensure_warmup_complete_degrades_on_timeout(monkeypatch):
    """Gate proceeds in degraded mode after timeout."""
    reset_warmup_gate()
    # Patch timeout to 0.1s for fast test
    monkeypatch.setattr("linkedin_mcp_server.drivers.browser._WARMUP_TIMEOUT", 0.1)
    # Should not raise — just logs warning and returns
    await ensure_warmup_complete()
    reset_warmup_gate()  # cleanup
```

Also in `tests/test_dependencies.py`, add a wiring test that verifies `ensure_warmup_complete` is called before `get_or_create_browser`:

```python
@pytest.mark.asyncio
async def test_get_extractor_calls_warmup_gate(self):
    """get_extractor calls ensure_warmup_complete before get_or_create_browser."""
    call_order = []

    async def mock_warmup():
        call_order.append("warmup_gate")

    async def mock_get_browser():
        call_order.append("get_browser")
        browser = MagicMock()
        browser.page = MagicMock()
        return browser

    with (
        patch("linkedin_mcp_server.dependencies.should_rotate", return_value=False),
        patch("linkedin_mcp_server.dependencies.ensure_warmup_complete", side_effect=mock_warmup),
        patch("linkedin_mcp_server.dependencies.get_or_create_browser", side_effect=mock_get_browser),
        patch("linkedin_mcp_server.dependencies.ensure_authenticated", new_callable=AsyncMock),
        patch("linkedin_mcp_server.dependencies.record_scrape"),
    ):
        from linkedin_mcp_server.dependencies import get_extractor
        async with get_extractor():
            pass

    assert call_order == ["warmup_gate", "get_browser"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_browser_driver.py::test_ensure_warmup_complete_returns_immediately_when_set tests/test_dependencies.py -v -k warmup`
Expected: FAIL (functions not yet defined / not imported)

- [ ] **Step 3: Add warm-up gate to drivers/browser.py**

In `linkedin_mcp_server/drivers/browser.py`, add near the top (after the globals section around line 55). Note: `asyncio` is not currently imported in this file — add `import asyncio` to the imports at the top:

```python
# Warm-up completion gate
_warmup_event = asyncio.Event()
_WARMUP_TIMEOUT = 120  # seconds


def mark_warmup_complete() -> None:
    """Signal that browser warm-up has finished."""
    _warmup_event.set()
    logger.info("Warm-up gate opened — tool calls may proceed")


async def ensure_warmup_complete() -> None:
    """Block until warm-up is complete or timeout (120s) expires.

    On timeout, logs a warning and allows tool calls in degraded mode.
    """
    if _warmup_event.is_set():
        return
    logger.info("Tool call waiting for warm-up to complete...")
    try:
        await asyncio.wait_for(_warmup_event.wait(), timeout=_WARMUP_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(
            "Warm-up gate timeout (%ds) — proceeding in degraded mode",
            _WARMUP_TIMEOUT,
        )


def reset_warmup_gate() -> None:
    """Reset the warm-up gate (for testing or browser reset)."""
    _warmup_event.clear()
```

- [ ] **Step 4: Call mark_warmup_complete in ALL code paths through get_or_create_browser**

There are **three** code paths in `get_or_create_browser()` that return a browser. ALL must call `mark_warmup_complete()` before returning:

1. **Source profile path** (`drivers/browser.py:415-424`) — uses `_authenticate_existing_profile`, NO warm-up. Add `mark_warmup_complete()` before `await start_background_navigation(browser.page)`:

```python
_browser = browser
_browser_cookie_export_path = cookie_path
mark_warmup_complete()  # No warm-up needed for source profile
await start_background_navigation(browser.page)
return _browser
```

2. **Fresh bridge path** (`drivers/browser.py:435-449`) — calls `_bridge_runtime_profile` which calls `warm_up_browser`. Add `mark_warmup_complete()` before `await start_background_navigation`:

```python
_browser = browser
_browser_cookie_export_path = None
mark_warmup_complete()
await start_background_navigation(browser.page)
return _browser
```

3. **Cached derived profile path** (`drivers/browser.py:471-481`) — uses `_authenticate_existing_profile`, NO warm-up. Add same pattern.

4. **Re-bridge fallback path** (`drivers/browser.py:499-513`) — calls `_bridge_runtime_profile`. Add same pattern.

In total, add `mark_warmup_complete()` before each of the 4 `await start_background_navigation()` calls in `get_or_create_browser()`.

Also add `reset_warmup_gate()` in `hard_reset_browser()` so the gate resets on context rotation:

```python
async def hard_reset_browser() -> None:
    await close_browser()
    reset_warmup_gate()  # Gate will re-open on next get_or_create_browser
    # ... rest unchanged
```

- [ ] **Step 5: Add ensure_warmup_complete to dependencies.py**

In `linkedin_mcp_server/dependencies.py`, update the import to include `ensure_warmup_complete`:

```python
from linkedin_mcp_server.drivers.browser import (
    ensure_authenticated,
    ensure_warmup_complete,
    get_or_create_browser,
    hard_reset_browser,
    record_scrape,
    should_rotate,
)
```

And in the `get_extractor` function, call it before `get_or_create_browser`:

```python
@asynccontextmanager
async def get_extractor() -> AsyncGenerator[LinkedInExtractor, None]:
    try:
        await ensure_warmup_complete()
        if should_rotate():
            logger.info("Context rotation threshold reached — resetting browser")
            await hard_reset_browser()
        browser = await get_or_create_browser()
        await ensure_authenticated()
        extractor = LinkedInExtractor(browser.page)
        try:
            yield extractor
        finally:
            record_scrape()
    except Exception as e:
        raise_tool_error(e, "get_extractor")
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/test_dependencies.py -v`
Expected: all PASS

- [ ] **Step 7: Run full test suite**

Run: `uv run pytest --tb=short`
Expected: all PASS

- [ ] **Step 8: Commit**

```bash
git add linkedin_mcp_server/drivers/browser.py linkedin_mcp_server/dependencies.py tests/test_dependencies.py
git commit -m "feat(warmup): add completion gate — tool calls block until warm-up finishes"
```

---

### Task 6: Integration Verification

**Files:**
- No new files — validation only

- [ ] **Step 1: Run linter**

Run: `uv run ruff check .`
Expected: no errors

- [ ] **Step 2: Run formatter**

Run: `uv run ruff format --check .`
Expected: no formatting issues (or run `uv run ruff format .` to fix)

- [ ] **Step 3: Run type checker**

Run: `uv run ty check`
Expected: no new errors

- [ ] **Step 4: Run full test suite with coverage**

Run: `uv run pytest --cov --tb=short`
Expected: all tests pass, coverage for modified files at 100%

- [ ] **Step 5: Manual smoke test of fingerprint audit (optional)**

Run: `python tools/fingerprint_server.py &` then open `http://localhost:8765` in Chrome, verify HTML page loads and collects fingerprint.

- [ ] **Step 6: Final commit if any fixups needed**

```bash
git add -A
git commit -m "chore: lint and format fixes for fingerprint audit + warm-up"
```
