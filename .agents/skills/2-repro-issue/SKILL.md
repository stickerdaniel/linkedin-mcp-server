---
name: 2-repro-issue
description: "Reproduce #N, investigate #N, try #N locally, or verify the bug in #N."
argument-hint: '<issue-number-or-url>'
---

# Investigate a LinkedIn-MCP issue

Evaluate the reporter packet and the matching source. A live LinkedIn call is optional. Do not check out a PR or attempt a fix. That is `/3-verify-pr-fix`.

## 1. Read the issue

```bash
NUM=$(echo "$ARGUMENTS" | sed -E 's|.*/||; s|#||g' | grep -oE '^[0-9]+' | head -1)
[ -z "$NUM" ] && { echo "Invalid input: '$ARGUMENTS'. Pass an issue number or URL." >&2; exit 1; }
REPO=stickerdaniel/linkedin-mcp-server

gh issue view $NUM --repo $REPO --comments
```

From the thread extract the tool, arguments, observed result, runtime, LinkedIn variant, and related issues. Distinguish observations, source findings, hypotheses, and work not run.

Map the tool to code:

1. `linkedin_mcp_server/tools/<surface>.py`. MCP entrypoint and arg validation
2. `docs/scraping-architecture.md`. Generated ownership table; follow it to `linkedin_mcp_server/scraping/<owner>.py` rather than treating `scraping/extractor.py` as the implementation
3. `linkedin_mcp_server/scraping/fields.py`. `PERSON_SECTIONS` / `COMPANY_SECTIONS` (each entry = one navigation)
4. The owner-local test, usually `tests/scraping/test_<owner>.py`; use `tests/test_fields.py`, `tests/test_identifiers.py`, and `tests/test_link_metadata.py` for those owners, and `tests/scraping/test_facade_*.py` only for facade contracts

## 2. Packet and source

Inspect the relevant source at the current SHA. Do not invent a LinkedIn result or an unimplemented tool call.

This step is complete when the report names the issue, the code SHA, the inspected evidence, the facts established, the unverified runtime claims, and the next decision.

Use one of:

- supported by reporter evidence
- confirmed in source
- needs more evidence (list the specific missing fields)
- not supported by the supplied evidence

A successful call on the maintainer's different account never refutes a failure on the reporter's account. Target content language is not the authenticated account's UI language. Captures and URL or attribute evidence may establish the needed variation without another live call.

Review reporter commands before execution. Publishing permission does not authorize `send_message`, a connection request, `react_to_post`, `comment_on_post`, `repost_post`, a forced login, or repeated calls with account side effects. Record `sent`, `recipient_selected`, `acted`, and `retry_safe` as observed fields, not as replay authorization. The post tools are public writes: a retried reaction can remove it, and a retried comment or repost can publish twice.

If the next decision needs a live observation, name the exact unresolved question and ask before login, session changes, or LinkedIn writes. If the human declines, keep the packet-and-source verdict.

## 3. Optional live check

Only after an explicit yes for this run. Use `uv run`, never `uvx`, so the server reflects the workspace. Record the actual tool, arguments, runtime, account variant, code SHA, and timestamp before the call. Keep the reporter's installed-launcher context distinct from a workspace check.

```bash
git status --porcelain | head -5
git log -1 --oneline
```

If the workspace is dirty, ask before continuing. If a login is required, ask; do not run `--login` as a default.

```bash
PORT=8765
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
echo $PORT > /tmp/repro-$NUM.port

uv run -m linkedin_mcp_server --transport streamable-http --port $PORT --log-level INFO > /tmp/repro-$NUM.log 2>&1 &
SERVER_PID=$!
echo $SERVER_PID > /tmp/repro-$NUM.pid

for i in $(seq 1 30); do
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
  kill -0 $SERVER_PID 2>/dev/null || { echo "Server died during startup. Tail of /tmp/repro-$NUM.log:" >&2; tail -20 /tmp/repro-$NUM.log >&2; exit 1; }
  sleep 1
done
lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || { echo "Server never bound port $PORT after 30s" >&2; tail -20 /tmp/repro-$NUM.log >&2; exit 1; }
```

```bash
PORT=$(cat /tmp/repro-$NUM.port)
curl -s -D /tmp/repro-$NUM-headers -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"repro-issue","version":"1.0"}}}' \
  > /tmp/repro-$NUM-init.json

SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/repro-$NUM-headers | awk '{print $2}' | tr -d '\r')
[ -z "$SESSION_ID" ] && { echo "MCP initialize returned no Mcp-Session-Id. Tail of /tmp/repro-$NUM.log:" >&2; tail -20 /tmp/repro-$NUM.log >&2; kill $SERVER_PID 2>/dev/null; exit 1; }
grep -q '"error"' /tmp/repro-$NUM-init.json && { echo "Initialize returned a protocol error. Execution limit." >&2; cat /tmp/repro-$NUM-init.json >&2; kill $SERVER_PID 2>/dev/null; exit 1; }

curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized","params":{}}' \
  > /tmp/repro-$NUM-initialized.json
grep -q '"error"' /tmp/repro-$NUM-initialized.json && { echo "notifications/initialized returned a protocol error. Execution limit." >&2; cat /tmp/repro-$NUM-initialized.json >&2; kill $SERVER_PID 2>/dev/null; exit 1; }
```

Capture and inspect both response bodies before `tools/call`. A protocol or validation error is an execution limit. Do not retry around it.

```bash
SHA=$(git rev-parse HEAD)
TOOL="<TOOL>"
ARGS_JSON='{<ARGS>}'

jq -n --arg t "$TOOL" --argjson a "$ARGS_JSON" --arg sha "$SHA" \
  '{tool: $t, arguments: $a, sha: $sha}' \
  > /tmp/repro-issue-$NUM-meta.json

curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS_JSON}}" \
  | tee /tmp/repro-issue-$NUM-main.json | head -200
```

If the issue does not pin a concrete target, do not invent one to complete the live check. Ask, or stop with the packet-and-source verdict.

Preserve `/tmp/repro-issue-$NUM-main.json` and `/tmp/repro-issue-$NUM-meta.json` only for a genuine captured run. A run from a non-main commit must identify that SHA in the meta file. Do not claim an on-main baseline because of the filename.

```bash
kill $SERVER_PID 2>/dev/null
wait $SERVER_PID 2>/dev/null
rm -f /tmp/repro-$NUM-headers /tmp/repro-$NUM.log /tmp/repro-$NUM.port /tmp/repro-$NUM.pid /tmp/repro-$NUM-init.json /tmp/repro-$NUM-initialized.json
```

Live verdicts, when a run happened:

- reproduced in the stated environment
- reproduced a different mode
- not reproduced in the maintainer environment (does not refute the reporter)
- execution limit (startup, protocol, login, rate limit)

## 4. Report

```
**#<N>**. <one-line issue summary>
**SHA:** <short-sha>
**Inspected:** <packet fields and source files>
**Facts established:** <list>
**Unverified:** <runtime claims not checked>
**Verdict:** <supported by reporter evidence | confirmed in source | reproduced in the stated environment | needs more evidence | not supported by the supplied evidence>
**Evidence:** <2 to 4 lines>
**Likely code path:** <file:line>. <one-line why>
**Baseline:** <path and SHA, or none>
**Next:** <missing fields | /3-verify-pr-fix N | fix sketch | no live check needed>
```

## Non-negotiables

- Packet and source first. Live LinkedIn only after yes, and only for a named unresolved question.
- `uv run`, not `uvx`, for a workspace live check. Use the reporter's launcher only to test that installation.
- One live run per invocation.
- Do not edit code, commit, or check out a PR.
- Do not create fake success or failure files to unlock `/3-verify-pr-fix`.
