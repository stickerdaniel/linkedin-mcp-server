# Fingerprint Audit + Warm-up Robustness

**Date:** 2026-03-21
**Status:** Draft
**Model:** Claude Opus 4.6

## Problem

The LinkedIn MCP server gets rate-limited/session-blocked on the very first tool call, even after the Docker container has been idle overnight. This suggests the issue is not request volume but rather **session/fingerprint detection** — LinkedIn marks the session as bot-like before any scraping occurs.

### Contributing factors

1. **Unknown fingerprint leaks** — We don't know what headers or JS fingerprints Playwright exposes that differ from a real Chrome browser. There may be signals we're not patching in `stealth.py`.
2. **Weak warm-up** — The current warm-up (`auth.py:37-100`) spends only 2-6s per page, has no scroll on external sites, and visits LinkedIn public pages (`/help`, `/about`, `/learning`) that no real user visits organically on startup.
3. **No warm-up gate** — Tool calls can proceed before warm-up completes, meaning the first LinkedIn navigation may happen with an under-warmed browser profile.
4. **AdGuard unknown** — The user runs AdGuard on the local network. It's unclear whether AdGuard modifies headers or DNS resolution in a way that affects Playwright differently than real Chrome.

## Solution

Two-phase approach: **diagnose first**, then **fix warm-up**.

### Phase A: Fingerprint Audit Tool

A diagnostic tool that compares what a remote server sees from Playwright vs. a real Chrome browser.

#### Components

**1. Diagnostic HTTP Server (`tools/fingerprint_server.py`)**

A standalone Python script using stdlib `http.server` that:

- Logs all incoming HTTP request headers
- Serves an HTML page with JavaScript fingerprint collection
- Accepts POST of client-side fingerprint results
- Saves results to timestamped JSON files in `tools/fingerprint_results/`

**2. Fingerprint Collection Page (`tools/fingerprint_page.html`)**

JavaScript that collects:

| Signal | Why it matters |
|--------|---------------|
| `navigator.webdriver` | Primary bot detection signal |
| `navigator.plugins` | Empty = headless indicator |
| `navigator.languages` | Must match Accept-Language header |
| `navigator.userAgent` | Must match HTTP User-Agent |
| `navigator.hardwareConcurrency` | Low values = VM/container |
| `navigator.deviceMemory` | Low values = VM/container |
| `navigator.platform` | Must be consistent with UA |
| WebGL renderer/vendor | SwiftShader = headless indicator |
| `performance.memory` | Missing in non-Chrome = indicator |
| Canvas fingerprint hash | Inconsistency across navigations = bot |
| `window.chrome` object | Missing = headless Chromium |
| Screen dimensions vs viewport | Mismatch = headless |
| `Notification.permission` | Default in headless |
| HTTP headers (server-side) | Extra or missing headers |

The page POSTs collected data back to the server as JSON.

**3. CLI Integration**

New flag `--fingerprint-audit` in `cli_main.py`:

```
uv run -m linkedin_mcp_server --fingerprint-audit
```

Behavior:
1. Start the diagnostic HTTP server on port 8765
2. Print instructions: "Open http://localhost:8765 in real Chrome to capture baseline"
3. Wait for baseline POST (user navigates manually)
4. Launch Playwright with identical config as production (same stealth scripts, same channel, same viewport)
5. Navigate Playwright to `http://localhost:8765`
6. Wait for Playwright POST
7. Generate side-by-side diff report to stdout
8. Save both JSONs and diff to `tools/fingerprint_results/`
9. Exit

**4. AdGuard Isolation**

The user runs the audit twice:
- Once with AdGuard active (normal network)
- Once with AdGuard bypassed (direct DNS)

The diff reveals whether AdGuard interferes with Playwright differently than Chrome.

### Phase B: Warm-up Robustness

Changes to `core/auth.py` warm_up_browser():

**1. Remove LinkedIn from initial warm-up**

Current code visits `/help`, `/about`, `/learning`, `/feed/` during warm-up. This is removed. The first LinkedIn contact should be the natural authenticated navigation triggered by the first tool call, not artificial public page visits.

The external sites list remains: Google, Wikipedia, GitHub.

**2. Increase dwell time**

Current: 2-6s random sleep per page.
New: 8-20s per page, with scroll and mouse movement on every page (not just LinkedIn).

```python
# Per external page:
# 1. Navigate (networkidle, 8s timeout)
# 2. Random mouse movements (3-5)
# 3. Scroll 3-6 times with 1-3s between scrolls
# 4. Hover 1-3 random links
# 5. Dwell 5-12s
# Total: ~15-35s per page
```

**3. Add scroll to external pages**

Current: only `/feed` gets scroll. New: all pages get scroll (Google results, Wikipedia articles, GitHub trending all have scrollable content).

**4. Warm-up completion gate**

New flag in the browser driver layer that tool calls check:

```python
# In drivers/browser.py or dependencies.py
_warmup_complete: bool = False

async def ensure_warmup_complete():
    """Block until warm-up has finished. Called before first tool execution."""
    if not _warmup_complete:
        # Wait with timeout
        ...
```

Tool calls block until warm-up completes. If warm-up fails entirely (0 sites reachable), log a warning but don't block indefinitely.

**5. Explicit success logging**

```
INFO: Warm-up complete: 3/3 external sites visited in 62s
```

This gives the user clear feedback in logs about warm-up status.

## Files Changed

| File | Change Type | Description |
|------|-------------|-------------|
| `tools/fingerprint_server.py` | New | Diagnostic HTTP server |
| `tools/fingerprint_page.html` | New | JS fingerprint collection page |
| `linkedin_mcp_server/cli_main.py` | Modified | Add `--fingerprint-audit` flag |
| `linkedin_mcp_server/core/auth.py` | Modified | Robust warm-up (remove LinkedIn, increase dwell, add scroll) |
| `linkedin_mcp_server/dependencies.py` or `linkedin_mcp_server/drivers/browser.py` | Modified | Warm-up completion gate |
| `tests/test_fingerprint_audit.py` | New | Tests for fingerprint server |
| `tests/test_auth.py` | Modified | Tests for new warm-up behavior |

## What's NOT in scope

- **Multi-profile rotation** — deferred until audit results reveal whether the problem is fingerprint-level (affects all profiles equally) or session-level (rotation would help)
- **Changes to stealth.py init scripts** — the audit will reveal if they need adjustment; that's a follow-up
- **Changes to background_nav.py** — the 30-60min loop is fine; the problem is the initial warm-up
- **Changes to context rotation threshold** — orthogonal to this issue

## Success Criteria

1. Fingerprint audit produces a clear diff showing all divergences between Playwright and real Chrome
2. AdGuard impact is isolated (run with/without)
3. Warm-up completes with validation before any tool call is accepted
4. Warm-up logs show explicit success/failure status with timing
