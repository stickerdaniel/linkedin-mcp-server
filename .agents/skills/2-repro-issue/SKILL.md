---
name: 2-repro-issue
description: Reproduce a single LinkedIn-MCP issue locally on the current branch against the real authenticated LinkedIn session at ~/.linkedin-mcp/profile/, using the MCP streamable-http server. Captures the exact failure mode (tool output, error, missing data) and maps it back to the scraper code path. Use when the user says "reproduce #N", "investigate #N", "try #N locally", "verify the bug in #N", or pastes an issue URL from stickerdaniel/linkedin-mcp-server. Does NOT check out a PR or attempt a fix — that's /3-verify-pr-fix.
argument-hint: '<issue-number-or-url>'
---

# Reproduce a LinkedIn-MCP Issue Locally

Goal: take an issue number, run the exact failing tool call against the real LinkedIn via the local MCP server, and produce concrete evidence — output JSON, error message, partial state — that confirms or refutes the bug on the current branch. Always use the authenticated profile already at `~/.linkedin-mcp/profile/`. Never mock.

## Phase 1 — Read and map

```bash
# Accept "442", "#442", or "https://github.com/.../issues/442" — extract the digits only
NUM=$(echo "$ARGUMENTS" | sed -E 's|.*/||; s|#||g' | grep -oE '^[0-9]+' | head -1)
[ -z "$NUM" ] && { echo "Invalid input: '$ARGUMENTS'. Pass an issue number or URL." >&2; exit 1; }
REPO=stickerdaniel/linkedin-mcp-server

gh issue view $NUM --repo $REPO --comments
```

From the issue body extract:

- **Which MCP tool** is affected (`get_person_profile`, `connect_with_person`, `search_jobs`, …). The issue templates ask for this explicitly.
- **The exact arguments** that trigger the failure (username, company slug, job ID, sections list).
- **The expected vs actual** behaviour.
- **Any locale signal** — German UI, non-English profile name, RTL language. Locale-sensitive bugs need a deliberately diverse target.

Map the tool to code so you know where to look if the repro confirms the bug:

1. `linkedin_mcp_server/tools/<surface>.py` — MCP entrypoint and arg validation
2. `linkedin_mcp_server/scraping/<feature>.py` — actual scraping (`extractor.py`, `connection.py`, `feed.py`, `inbox.py`, …)
3. `linkedin_mcp_server/scraping/fields.py` — `PERSON_SECTIONS` / `COMPANY_SECTIONS` (each entry = one navigation)
4. Existing test in `tests/test_scraping.py` covering the same surface

State out loud before running anything: "Reproducing tool `X` with args `Y` on branch `<current>` — expecting `<failure mode from issue>`."

## Phase 2 — Confirm session, branch, dependencies

```bash
git status --porcelain | head -5         # workspace must be clean
git log -1 --oneline                     # record the SHA we're testing
ls ~/.linkedin-mcp/profile/ | head -3    # profile must exist
```

If the workspace is dirty, ask the user before continuing — they may have local changes that affect the repro. If the profile is missing or stale, run `uv run -m linkedin_mcp_server --login` once and only once per skill invocation.

## Phase 3 — Run the MCP server

Always `uv run`, never `uvx` — the running server must reflect the current workspace (per `CLAUDE.md → Verifying Bug Reports`).

Default port 8000 is commonly taken by other dev servers (workspace-mcp, etc.). Probe for a free port from 8765 upward instead of failing late on `address already in use`:

```bash
# Probe a free port starting at 8765
PORT=8765
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
echo $PORT > /tmp/repro-$NUM.port

# Start in background
uv run -m linkedin_mcp_server --transport streamable-http --port $PORT --log-level INFO > /tmp/repro-$NUM.log 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > /tmp/repro-$NUM.pid

# Wait for the port to actually start LISTENing instead of a blind sleep.
# Cap at ~30s so a stuck startup doesn't hang the run.
for i in $(seq 1 30); do
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
  kill -0 $SERVER_PID 2>/dev/null || { echo "Server died during startup. Tail of /tmp/repro-$NUM.log:" >&2; tail -20 /tmp/repro-$NUM.log >&2; exit 1; }
  sleep 1
done
lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || { echo "Server never bound port $PORT after 30s" >&2; tail -20 /tmp/repro-$NUM.log >&2; exit 1; }
echo "Server PID: $SERVER_PID  Port: $PORT  Ready"
```

If `/tmp/repro-$NUM.log` shows login failure or browser issues, stop and report, do not try to brute-force around it.

## Phase 4 — Initialize MCP session

```bash
PORT=$(cat /tmp/repro-$NUM.port)
curl -s -D /tmp/repro-$NUM-headers -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"repro-issue","version":"1.0"}}}' > /dev/null

SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/repro-$NUM-headers | awk '{print $2}' | tr -d '\r')
[ -z "$SESSION_ID" ] && { echo "MCP initialize returned no Mcp-Session-Id. Tail of /tmp/repro-$NUM.log:" >&2; tail -20 /tmp/repro-$NUM.log >&2; kill $SERVER_PID 2>/dev/null; exit 1; }

curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized","params":{}}' > /dev/null
```

The `notifications/initialized` post often returns `{"error":{"code":-32602,"message":"Invalid request parameters"}}` and the server log shows a long pydantic ClientRequest validation dump. **This is harmless.** The session is still valid and `tools/call` works on the same `SESSION_ID`. Do not retry, do not treat as a session failure.

## Phase 5 — Call the failing tool

Substitute the tool name and arguments from Phase 1 verbatim. Save the response to a stable path **and** persist the request metadata so `/3-verify-pr-fix` can replay the exact same call without guessing.

```bash
TOOL="<TOOL>"                 # e.g. get_person_profile
ARGS_JSON='{<ARGS>}'           # e.g. {"linkedin_username":"williamhgates","sections":"basic_info"}

# Persist what we are about to call so the verifier can re-run identically
jq -n --arg t "$TOOL" --argjson a "$ARGS_JSON" '{tool: $t, arguments: $a}' \
  > /tmp/repro-issue-$NUM-meta.json

curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS_JSON}}" \
  | tee /tmp/repro-issue-$NUM-main.json | head -200
```

If the issue doesn't pin a concrete target, pick a stable public one:

- Person tools: `williamhgates`, plus one non-English target if locale matters.
- Company tools: `microsoft`, plus one German/EU company if locale matters.
- Search tools: a query from the issue, else a deliberately diverse phrase.

Run the call twice if the result looks flaky (network, slow render) — LinkedIn rate-limit / partial-render can mask real bugs.

## Phase 6 — Classify

Read `/tmp/repro-issue-$NUM-main.json` and the server log. Pick one verdict:

- **Reproduced ✓** — failure matches the issue description. Note exactly what's missing/wrong (which section is empty, which reference field is null, which error is raised).
- **Reproduced different mode ⚠** — there is a problem but it doesn't match the issue exactly. Could be a related-but-distinct bug.
- **Not reproduced ✗** — tool returned expected data. The bug may already be fixed in an unreleased commit (check `git log --oneline -20` and last release tag) or the issue may be environment-specific (different LinkedIn account, different locale).
- **Inconclusive** — network/rate-limit/login error, not a code-level signal.

Locate the failure in code:

- Grep for the tool name: `grep -rn "def <tool>" linkedin_mcp_server/`
- Trace from tool entrypoint into the scraper module.
- For empty-section bugs, check `scraping/fields.py` for the section's URL and verify it's correct.

## Phase 7 — Report and clean up

```bash
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
rm -f /tmp/repro-$NUM-headers /tmp/repro-$NUM.log /tmp/repro-$NUM.port /tmp/repro-$NUM.pid
# Keep /tmp/repro-issue-$NUM-main.json (response baseline) and
# /tmp/repro-issue-$NUM-meta.json (tool + args) — /3-verify-pr-fix uses both.
```

Report format:

```
**#<N>** — <one-line issue summary>
**Branch / SHA:** <branch> @ <short-sha>
**Tool / args:** <TOOL>(<ARGS>)
**Verdict:** <Reproduced ✓ | Reproduced different mode ⚠ | Not reproduced ✗ | Inconclusive>
**Evidence:** <2–4 lines of the actual tool output that proves the verdict>
**Likely code path:** <file:line> — <one-line why>
**Baseline saved at:** /tmp/repro-issue-<N>-main.json (used by /3-verify-pr-fix)
**Next:** <suggest /3-verify-pr-fix N if a candidate PR exists | suggest fix sketch | suggest closing as already-fixed>
```

## Non-negotiables

- Real LinkedIn against the real profile. No mocks, no fixtures.
- `uv run`, not `uvx`. The server must reflect the workspace.
- One run per skill invocation — don't repeatedly hammer LinkedIn.
- Do not edit code, do not commit, do not check out a PR. This skill only reproduces and reports.
- If the workspace is dirty, ask before continuing — local changes can hide or fake the bug.
