#!/usr/bin/env python3
"""Capture Playwright fingerprint and compare with Chrome baseline.

Usage:
    uv run tools/run_playwright_fingerprint.py [baseline_json_path]

If baseline_json_path is omitted, uses the most recent baseline file in
tools/fingerprint_results/.
"""

import asyncio
import json
import sys
from pathlib import Path

# Add project root to path so we can import linkedin_mcp_server
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

RESULTS_DIR = Path(__file__).parent / "fingerprint_results"

_FINGERPRINT_JS = """
async () => {
  const fp = {};
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
  fp.pluginCount = navigator.plugins.length;
  fp.plugins = [];
  for (let i = 0; i < navigator.plugins.length; i++) {
    fp.plugins.push({
      name: navigator.plugins[i].name,
      filename: navigator.plugins[i].filename,
      description: navigator.plugins[i].description,
    });
  }
  fp.screen = {
    width: screen.width, height: screen.height,
    availWidth: screen.availWidth, availHeight: screen.availHeight,
    colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
  };
  fp.innerWidth = window.innerWidth;
  fp.innerHeight = window.innerHeight;
  fp.outerWidth = window.outerWidth;
  fp.outerHeight = window.outerHeight;
  fp.devicePixelRatio = window.devicePixelRatio;
  fp.hasWindowChrome = typeof window.chrome !== 'undefined';
  fp.windowChromeKeys = typeof window.chrome === 'object' && window.chrome !== null
    ? Object.keys(window.chrome) : null;
  fp.hasPerformanceMemory = typeof performance.memory !== 'undefined';
  fp.performanceMemory = performance.memory ? {
    jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
    totalJSHeapSize: performance.memory.totalJSHeapSize,
    usedJSHeapSize: performance.memory.usedJSHeapSize,
  } : null;
  fp.notificationPermission = typeof Notification !== 'undefined'
    ? Notification.permission : null;
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
    } else { fp.webgl = null; }
  } catch (e) { fp.webgl = { error: e.message }; }
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 200; canvas.height = 50;
    const ctx = canvas.getContext('2d');
    ctx.textBaseline = 'top'; ctx.font = '14px Arial';
    ctx.fillStyle = '#f60'; ctx.fillRect(125, 1, 62, 20);
    ctx.fillStyle = '#069'; ctx.fillText('Fingerprint', 2, 15);
    ctx.fillStyle = 'rgba(102, 204, 0, 0.7)'; ctx.fillText('Fingerprint', 4, 17);
    fp.canvasHash = canvas.toDataURL().length;
  } catch (e) { fp.canvasHash = null; }
  fp.timezoneOffset = new Date().getTimezoneOffset();
  fp.timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  fp.connection = navigator.connection ? {
    effectiveType: navigator.connection.effectiveType,
    downlink: navigator.connection.downlink,
    rtt: navigator.connection.rtt,
  } : null;
  return fp;
}
"""


async def capture_playwright_fingerprint() -> dict:
    from linkedin_mcp_server.core.browser import BrowserManager
    from linkedin_mcp_server.config import get_config

    config = get_config()
    browser_config = config.browser

    audit_profile = Path.home() / ".linkedin-mcp" / "audit-profile"
    async with BrowserManager(
        user_data_dir=audit_profile,
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
        await page.goto("about:blank", wait_until="domcontentloaded", timeout=10000)
        fp = await page.evaluate(_FINGERPRINT_JS)
        return fp


def print_diff(baseline_path: Path, playwright_fp: dict) -> None:
    baseline_data = json.loads(baseline_path.read_text())
    baseline_fp = baseline_data["client_fingerprint"]

    all_keys = sorted(set(list(baseline_fp.keys()) + list(playwright_fp.keys())))

    print("\n" + "=" * 70)
    print("FINGERPRINT DIFF: Chrome (baseline) vs Playwright")
    print("=" * 70)

    diffs = 0
    for key in all_keys:
        b_val = baseline_fp.get(key, "<missing>")
        p_val = playwright_fp.get(key, "<missing>")
        if b_val != p_val:
            diffs += 1
            print(f"\n  {key}:")
            print(f"    Chrome:     {json.dumps(b_val)}")
            print(f"    Playwright: {json.dumps(p_val)}")

    if diffs == 0:
        print("\n  (no differences in client fingerprint)")

    print(f"\nTotal: {diffs} fingerprint difference(s)")
    print("=" * 70)

    # Save results
    from datetime import datetime, timezone
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    pw_path = RESULTS_DIR / f"{ts}-playwright.json"
    pw_path.write_text(json.dumps({"source": "playwright", "client_fingerprint": playwright_fp}, indent=2))
    print(f"\nPlaywright result saved to {pw_path}")

    diff_lines = []
    for key in all_keys:
        b_val = baseline_fp.get(key, "<missing>")
        p_val = playwright_fp.get(key, "<missing>")
        if b_val != p_val:
            diff_lines.append(f"  {key}: Chrome={json.dumps(b_val)} | Playwright={json.dumps(p_val)}")
    diff_path = RESULTS_DIR / f"{ts}-diff.txt"
    diff_path.write_text("\n".join(diff_lines))
    print(f"Diff saved to {diff_path}")


def find_baseline() -> Path:
    if not RESULTS_DIR.exists():
        print(f"No results directory at {RESULTS_DIR}")
        sys.exit(1)
    baselines = sorted(RESULTS_DIR.glob("*-baseline.json"), reverse=True)
    if not baselines:
        print(f"No baseline files found in {RESULTS_DIR}")
        print("Run: python tools/fingerprint_server.py and open http://localhost:8765 in Chrome first")
        sys.exit(1)
    return baselines[0]


def main() -> None:
    if len(sys.argv) > 1:
        baseline_path = Path(sys.argv[1])
    else:
        baseline_path = find_baseline()

    print(f"Using baseline: {baseline_path}")
    print("Launching Playwright to capture fingerprint...")

    playwright_fp = asyncio.run(capture_playwright_fingerprint())
    print("Playwright fingerprint captured.")

    print_diff(baseline_path, playwright_fp)


if __name__ == "__main__":
    main()
