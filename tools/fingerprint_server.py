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
        import io
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        diff_path = RESULTS_DIR / f"{timestamp}-diff.txt"
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
