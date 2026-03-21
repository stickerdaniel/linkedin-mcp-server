# Full Stealth Overhaul — Playwright Anti-Detection

**Date:** 2026-03-20
**Status:** Draft
**Author:** Claude Opus 4.6 + Andre

---

## Problem

CAPTCHAs appear frequently at startup in Docker environments, even with the existing warm-up flow running. Root causes identified through codebase analysis and web research:

1. **Docker fingerprint mismatch** — timezone=UTC, no locale, no Accept-Language header. LinkedIn cross-references these with imported cookies (created on macOS with different TZ/locale) and flags the inconsistency.
2. **Shallow warm-up** — uses `domcontentloaded` (doesn't fully load resources), no mouse movement, minimal feed scrolling (2-4 scrolls), no element interactions.
3. **Zero idle activity** — browser sits completely idle between MCP tool calls. LinkedIn detects "new browser + burst scraping" pattern.
4. **Missing fingerprint hardening** — no init scripts for navigator.plugins, WebGL vendor, or performance.memory. Headless browser exposes bot-like values.

## Goal

Make the Docker browser indistinguishable from a real Chrome used by a human, reducing CAPTCHA triggers on startup.

## Research Sources

- [ScrapeOps — Make Playwright Undetectable](https://scrapeops.io/playwright-web-scraping-playbook/nodejs-playwright-make-playwright-undetectable/)
- [BrowserStack — Playwright Bot Detection](https://www.browserstack.com/guide/playwright-bot-detection)
- [ZenRows — Playwright Fingerprinting](https://www.zenrows.com/blog/playwright-fingerprint)
- [Oboe — LinkedIn CAPTCHA Evasion](https://oboe.com/learn/high-scale-linkedin-data-extraction-w8oh2/evasive-captcha-handling-5)
- [DEV Community — Stealth Browser for AI Agents](https://dev.to/bridgeace/stealth-browser-how-ai-agents-bypass-bot-detection-3eh6)

---

## Architecture

### Phase 1: Fix Docker Fingerprint Gaps (HIGH PRIORITY)

**Files:** `config/schema.py`, `config/loaders.py`, `core/browser.py`, `drivers/browser.py`, `Dockerfile`

Add 3 fields to `BrowserConfig`:
- `locale: str = "en-US"` — affects `navigator.language`, HTML lang attribute
- `timezone_id: str = "America/Sao_Paulo"` — affects `Date.toString()`, timezone APIs
- `accept_language: str = "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"` — HTTP Accept-Language header

These are passed to Playwright's `launch_persistent_context()` as `locale` and `timezone_id` context options. `Accept-Language` is set via `context.set_extra_http_headers()`.

Environment variable overrides: `LOCALE`, `TIMEZONE_ID`, `ACCEPT_LANGUAGE` — for users in other regions.

Dockerfile updated:
```dockerfile
ENV TZ=America/Sao_Paulo
ENV LANG=pt_BR.UTF-8
ENV LC_ALL=pt_BR.UTF-8
```

### Phase 2: Stealth Init Scripts + Mouse Simulation (HIGH PRIORITY)

**Files:** `core/stealth.py` (NEW), `core/browser.py`

New module `core/stealth.py` with:

**`get_stealth_init_scripts() -> list[str]`** — returns JS scripts injected via `context.add_init_script()` before any navigation:
- `navigator.webdriver = undefined` (defense-in-depth; Patchright already patches this)
- `navigator.plugins` injection: Chrome PDF Plugin, Chrome PDF Viewer, Native Client
- WebGL `getParameter` spoof: vendor="Google Inc. (NVIDIA)", renderer="ANGLE (NVIDIA, ...)"
- `performance.memory` with realistic values (jsHeapSizeLimit, totalJSHeapSize, usedJSHeapSize)
- Each script checks current value before patching (no-op if Patchright already fixed it)

**`random_mouse_move(page, count=3)`** — moves cursor to random viewport positions with small delays between movements.

**`hover_random_links(page, max_links=2)`** — finds visible `<a>` elements, hovers over 1-2 random ones with natural timing.

Browser arg added: `--disable-blink-features=AutomationControlled`

### Phase 3: Enhanced Warm-Up (MEDIUM PRIORITY)

**Files:** `core/auth.py`

Changes to existing `warm_up_browser()`:
1. `wait_until="networkidle"` with 8000ms timeout (graceful fallback on timeout)
2. Call `random_mouse_move(page)` after each navigation
3. Feed scroll range: `randint(5, 10)` (was 2-4)
4. Call `hover_random_links(page)` on LinkedIn pages
5. Wider delays: `uniform(2.0, 6.0)` between sites (was 1.5-4.0)

### Phase 4: Background Periodic Navigation (MEDIUM PRIORITY)

**Files:** `core/browser_lock.py` (NEW), `core/background_nav.py` (NEW), `sequential_tool_middleware.py`, `drivers/browser.py`, `server.py`

**Shared lock** (`core/browser_lock.py`): Extract `asyncio.Lock` from `SequentialToolExecutionMiddleware` into shared module. Both the middleware and background nav import the same lock instance.

**Background navigation** (`core/background_nav.py`):
- Asyncio task running while server is active
- Interval: `random.uniform(30*60, 60*60)` seconds (30-60 min)
- Navigates 1-3 random sites per cycle
- **NO LinkedIn** — avoid session blocks/CAPTCHAs
- Site pool: Google (random queries), YouTube, Wikipedia Random, HackerNews, GitHub Trending, Stack Overflow, Medium
- Interactions per site: scroll 2-5x, hover on links, "reading time" 5-15s
- Acquires `browser_lock` before navigating (coordinates with active tool calls)
- Returns to `about:blank` after each cycle
- Catches all exceptions (never crash the loop)
- Graceful shutdown via `asyncio.Task.cancel()`

**Lifecycle integration:**
- Start after successful authentication in `drivers/browser.py`
- Stop on server shutdown in `server.py`

---

## Data Flow

```
Server Start
  -> Authentication
  -> Warm-Up (enhanced: networkidle, mouse moves, more scrolls)
  -> Start Background Nav Task
  -> Ready for MCP Tool Calls

Tool Call arrives:
  -> Acquire browser_lock
  -> Execute scraping
  -> Release browser_lock

Background Nav (every 30-60min):
  -> Acquire browser_lock
  -> Navigate random site (NOT LinkedIn)
  -> Scroll, hover, "read"
  -> Return to about:blank
  -> Release browser_lock

Server Shutdown:
  -> Cancel Background Nav Task
  -> Close Browser
```

---

## Files Changed

| File | Action | Phase |
|------|--------|-------|
| `config/schema.py` | Edit — 3 new fields | 1 |
| `config/loaders.py` | Edit — 3 env vars | 1 |
| `core/browser.py` | Edit — locale/tz/headers + init scripts + args | 1, 2 |
| `drivers/browser.py` | Edit — wire config + start background nav | 1, 4 |
| `Dockerfile` | Edit — TZ/LANG/locales | 1 |
| `core/stealth.py` | **NEW** — init scripts + mouse sim | 2 |
| `core/auth.py` | Edit — enhanced warm-up | 3 |
| `core/browser_lock.py` | **NEW** — shared lock | 4 |
| `core/background_nav.py` | **NEW** — periodic navigation | 4 |
| `sequential_tool_middleware.py` | Edit — use shared lock | 4 |
| `server.py` | Edit — stop background nav | 4 |

## Tests

| Test File | What It Covers |
|-----------|---------------|
| `tests/test_config.py` | New field defaults + env var loading |
| `tests/test_core_browser.py` | locale/timezone passed to context, headers set, init scripts applied, browser args |
| `tests/test_browser_driver.py` | New fields wired in `_make_browser()` |
| `tests/test_stealth.py` (NEW) | Init scripts return valid JS, mouse.move called N times, hover sequence |
| `tests/test_core_auth.py` | networkidle used, mouse_move called, scroll range 5-10 |
| `tests/test_background_nav.py` (NEW) | Lock acquired, stop cancels task, errors don't crash, interval 30-60min, URL restored |
| `tests/test_sequential_tool_middleware.py` | Middleware works with shared lock |

---

## Verification

1. `uv run pytest --cov` — 100% coverage on new modules
2. `uv run ruff check . && uv run ruff format --check .`
3. `uv run ty check`
4. E2E local: `uv run -m linkedin_mcp_server --no-headless` — verify mouse movements visible during warm-up
5. E2E Docker: `docker build && docker run` — verify TZ, locale, headers in DevTools
6. Fingerprint test: navigate to `bot.sannysoft.com` during warm-up
7. Background nav: verify in logs that periodic navigation occurs every 30-60min without interfering with tool calls

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| `networkidle` timeout in warm-up | 8000ms timeout + graceful fallback |
| Background nav holds lock when tool call arrives | Short navigations (5-15s), infrequent (30-60min) |
| Init scripts conflict with Patchright | Each script checks value before patching |
| `locales` package increases Docker image | Generate only `pt_BR.UTF-8` (~15MB) |
