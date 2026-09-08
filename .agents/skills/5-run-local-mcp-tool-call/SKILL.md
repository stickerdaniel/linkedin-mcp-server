---
name: 5-run-local-mcp-tool-call
description: Start this repo's MCP server locally (streamable-http transport) and drive a real tool call (e.g. send_message, create_post, search_people) via curl against the running instance, using the existing persistent LinkedIn browser profile. Use when the user asks to "try/test" a tool live, "send a message to X", "post something as a test", or otherwise wants an actual LinkedIn action executed rather than just code changes. Not for verifying a candidate PR fix (use /3-verify-pr-fix for that) and not for opening/updating a PR (use /4-address-pr-review for that).
argument-hint: '<tool-name> <json-arguments>'
---

# Run a Live MCP Tool Call Against the Local Server

Goal: get from "try calling tool X with these args" to an actual executed LinkedIn action (or a clear, actionable error) with minimal back-and-forth, reusing the existing authenticated persistent profile at `~/.linkedin-mcp/profile` rather than re-logging in every time.

## Phase 0 — Identify the target unambiguously before any write/destructive tool

For any tool with `destructiveHint: true` (`send_message`, `create_post`, `connect_with_person`, etc.), **never** guess at a recipient/target from a name alone. Run a read-only lookup first (`search_people`, `get_person_profile`, `get_company_profile`) and require an *unambiguous* match — a name, headline, mutual-connections count, or vanity URL that clearly identifies one person/page — before proceeding to the write call. If the search returns multiple plausible matches (e.g. two people with the same or similar names) and nothing else disambiguates them, stop and ask the user for the exact profile URL/username rather than picking one.

## Phase 1 — Start the server

```bash
cd <repo>
.venv\Scripts\python.exe -m linkedin_mcp_server --transport streamable-http --port 8765 --log-level INFO --browser-idle-timeout 0
```

Run this **detached** (`mode: async, detach: true` in this environment) since it's a long-lived process the user will want to keep across multiple tool calls in the same session, not a one-shot command.

Pick a free port; 8000 is commonly already in use. Wait ~10s for "StreamableHTTP session manager started" before calling the endpoint.

### Known trap: browser profile version mismatch → "Target page, context or browser has been closed"

If the persistent profile (`~/.linkedin-mcp/profile`) was last opened by a **different/newer Chrome build** than the server's bundled Patchright Chromium, Chrome's own downgrade-protection logic tries to migrate/quarantine cache directories and the launch fails with errors like:

```
ERROR:chrome\browser\downgrade\downgrade_utils.cc:36] ...\GPUPersistentCache -> ...CHROME_DELETE\GPUPersistentCache: Access is denied.
Failed to start browser: BrowserType.launch_persistent_context: Target page, context or browser has been closed
```

This is **not** a locked-profile issue (check with `Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*<profile-path>*' }` to confirm nothing is actually holding it open before assuming otherwise). The fix is to restart the server pointing `--chrome-path` at the actual installed Chrome binary that created/last touched the profile (commonly `C:\Program Files\Google\Chrome\Application\chrome.exe` on Windows), rather than letting it fall back to the bundled Chromium:

```bash
.venv\Scripts\python.exe -m linkedin_mcp_server --transport streamable-http --port 8765 --log-level INFO --browser-idle-timeout 0 --chrome-path "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

## Phase 2 — Initialize an MCP session

```bash
curl -sS -D headers.txt -o init.txt -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"copilot-cli","version":"1.0"}}}'
# Extract Mcp-Session-Id from headers.txt, then:
curl -sS -o NUL -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}'
```

`notifications/initialized` commonly returns a harmless pydantic validation error in the response body — ignore it, the session ID still works for subsequent `tools/call` requests.

## Phase 3 — Call the tool

```bash
curl -sS -X POST http://127.0.0.1:8765/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS_JSON}}"
```

Check exact keyword-argument names against the tool's Python signature before calling — FastMCP surfaces a pydantic `unexpected_keyword_argument` error immediately for anything not accepted (e.g. `search_people` takes no `max_pages`, only `search_posts` does).

### Known trap: session expiry mid-flow needs a real wait, not just a retry

If a call returns `"Session expired. A login browser window has been opened. Sign in..."`, a headed browser window opened for manual login. Two things must both be true before retrying:

1. The user has actually completed sign-in in that window (ask them to confirm — do not assume).
2. Enough wall-clock time has passed for the server's login-detection loop to notice. It polls roughly every 30s (`"Still waiting for manual login..."` in the server log) and only writes `"Manual login completed successfully"` + re-exports cookies once it detects the change — retrying the tool call within a few seconds of the user confirming can still race ahead of that detection and return `"login is still in progress"`. Tail the server's log file (`--log-level INFO` output, or the `.log` file if run detached) for the `"Manual login completed successfully"` line before retrying, rather than guessing based on elapsed time alone.

## Phase 4 — Report and clean up

Report the tool's raw JSON result (status, message, whatever identifying info like `url`/`recipient_selected`/`posted` it returns) — don't just say "done". For any destructive action, explicitly confirm which target it hit (profile URL, page name) so the user can verify it went to the right place.

Stop the detached server process (`stop_powershell` on its shellId) once the user is done testing, unless they indicate they want it left running for further calls.

## Non-negotiables

- Never call a destructive tool against a name-only, unverified target — resolve to an unambiguous profile/page first (Phase 0).
- Don't guess-fix profile/browser errors — read the actual error text and server log; "Access is denied" + downgrade_utils.cc means a Chrome-version mismatch, not a permissions bug to route around with elevated privileges.
- Don't add new messaging/posting code paths without first checking whether the existing tool (`send_message`, `create_post`, etc.) already covers the request — this repo already has full messaging and posting tools; confirm via `grep -n "async def send_message\|async def create_post"` in `linkedin_mcp_server/tools/` before assuming something needs to be built.
