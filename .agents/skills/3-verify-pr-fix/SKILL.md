---
name: 3-verify-pr-fix
description: "Verify PR #N, does this PR fix #M, check the fix in #N, or test the candidate PR."
argument-hint: '<pr-number-or-url>'
---

# Verify a candidate PR

Review the packet, the candidate diff, and relevant tests. Live comparison against a captured baseline is optional. No edits, no merges, no pushes.

## 1. Resolve the PR and its linked issue

```bash
PR=$(echo "$ARGUMENTS" | sed -E 's|.*/||; s|#||g' | grep -oE '^[0-9]+' | head -1)
[ -z "$PR" ] && { echo "Invalid input: '$ARGUMENTS'. Pass a PR number or URL." >&2; exit 1; }
REPO=stickerdaniel/linkedin-mcp-server

gh pr view $PR --repo $REPO --json title,body,baseRefName,headRefName,headRepositoryOwner,mergeable,mergeStateStatus,additions,deletions,changedFiles,maintainerCanModify,statusCheckRollup
```

Extract the linked issue number from the PR body (`Closes #`, `Fixes #`, `Resolves #`, or plain `#N`). Call it `ISSUE`. If multiple, ask which one is the verification target.

Read that issue's packet and comments.

## 2. Packet, diff, and tests

This path does not need `/tmp/repro-issue-$ISSUE-main.json`. Absence of a local baseline limits the live branch, not the whole PR verdict. Do not create fake success or failure files.

```bash
gh pr diff $PR --repo $REPO
gh pr view $PR --repo $REPO --json files --jq '.files[].path'
```

Hard flags (any one downgrades a clean live pass to concerns):

- Locale-dependent detection: `== "Connect"`, `in ["Pending", "Follow"]`, `contains("1st")`, `aria-label="..."` with translated text. Attribute presence is the locale-independent signal.
- LinkedIn class-name selectors: `.entity-result__item`, `.artdeco-button__text`. Minimal generic selectors only (`a[href*="/jobs/view/"]`).
- Multiple navigations behind one `PERSON_SECTIONS` / `COMPANY_SECTIONS` entry.
- Missing tests beside the canonical owner from `docs/scraping-architecture.md`.

A supplied capture or regression fixture can support a before-and-after claim within its scope. Protocol or startup failures are execution limits, not a pass.

This step is complete when the report can say whether the diff addresses the packet at this SHA, which audit flags apply, and whether a live comparison is still needed.

## 3. Optional live comparison

Only after an explicit yes, and only when a genuine captured baseline exists.

`/2-repro-issue` writes:

- `/tmp/repro-issue-<ISSUE>-main.json`. Captured response
- `/tmp/repro-issue-<ISSUE>-meta.json`. `{tool, arguments, sha}`

If either file is missing or empty, keep the packet-and-diff verdict. Offer the evidence-and-code path. A new live baseline requires approval.

Read `sha` from the meta file. Do not claim an on-main baseline merely because of the filename. Compare the same call and a sufficiently comparable account context before claiming a live fix.

```bash
[ -s /tmp/repro-issue-$ISSUE-main.json ] || { echo "No captured baseline. Packet-and-diff verdict stands." >&2; }
[ -s /tmp/repro-issue-$ISSUE-meta.json ] || { echo "No captured meta. Packet-and-diff verdict stands." >&2; }
TOOL=$(jq -r .tool /tmp/repro-issue-$ISSUE-meta.json)
ARGS_JSON=$(jq -c .arguments /tmp/repro-issue-$ISSUE-meta.json)
BASE_SHA=$(jq -r .sha /tmp/repro-issue-$ISSUE-meta.json)
echo "Replaying: $TOOL($ARGS_JSON) from sha $BASE_SHA"
```

Guarded checkout. Leave the user's original branch or detached SHA intact. Clean up only processes and files this run created.

```bash
git status --porcelain | head -5
CURRENT_REF=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)

git fetch origin "pull/$PR/head" || { echo "git fetch for PR #$PR failed, aborting before checkout." >&2; exit 1; }
PR_SHA=$(git rev-parse FETCH_HEAD)
git checkout --detach "$PR_SHA"

cleanup_verify() {
  rc=$?
  trap - EXIT INT TERM
  kill $SERVER_PID 2>/dev/null
  wait $SERVER_PID 2>/dev/null
  git checkout "$CURRENT_REF" 2>/dev/null
  rm -f /tmp/verify-pr-$PR.json /tmp/verify-pr-$PR-headers /tmp/verify-pr-$PR.log
  exit $rc
}
trap cleanup_verify EXIT INT TERM
```

Restart the server after checkout.

```bash
PORT=8765
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
uv run -m linkedin_mcp_server --transport streamable-http --port $PORT --log-level INFO > /tmp/verify-pr-$PR.log 2>&1 &
SERVER_PID=$!

for i in $(seq 1 30); do
  lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && break
  kill -0 $SERVER_PID 2>/dev/null || { echo "Server died during startup. Tail of /tmp/verify-pr-$PR.log:" >&2; tail -20 /tmp/verify-pr-$PR.log >&2; exit 1; }
  sleep 1
done
lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || { echo "Server never bound port $PORT after 30s" >&2; tail -20 /tmp/verify-pr-$PR.log >&2; exit 1; }

curl -s -D /tmp/verify-pr-$PR-headers -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify-pr","version":"1.0"}}}' > /dev/null

SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/verify-pr-$PR-headers | awk '{print $2}' | tr -d '\r')
[ -z "$SESSION_ID" ] && { echo "MCP initialize returned no Mcp-Session-Id. Tail of /tmp/verify-pr-$PR.log:" >&2; tail -20 /tmp/verify-pr-$PR.log >&2; kill $SERVER_PID 2>/dev/null; exit 1; }

curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized","params":{}}' > /dev/null
```

If initialize or `notifications/initialized` returns a protocol or validation error, report it as an execution limit.

```bash
curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS_JSON}}" \
  | tee /tmp/verify-pr-$PR.json | head -200

kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
diff -u /tmp/repro-issue-$ISSUE-main.json /tmp/verify-pr-$PR.json | head -200
```

Live outcomes: fixes in this environment, fixes with concerns, does not fix in this environment, execution limit. A success here never refutes a reporter failure on a different account.

## 4. Report

```
**PR #<PR>** (linked #<ISSUE>). <one-line PR title>
**Mergeable:** <CLEAN | DIRTY conflicts | BLOCKED>
**Scope:** <+X/-Y, N files>
**Packet/diff verdict:** <addresses the packet | does not address the packet | needs more evidence>
**Live verdict:** <optional, or skipped: no baseline>
**Audit flags:** <locale-dependent | DOM-class selectors | section-mapping violation | missing tests | none>
**Recommended next step:** <merge | request changes | take over via maintainer-edits | no live check needed>
```

## Non-negotiables

- Packet and diff first. Live comparison only after yes, with a genuine baseline.
- Restart the server after checkout. Running workers hold stale code.
- Do not push, edit the PR, or merge.
- Leave the user's original branch or detached SHA intact.
- Locale and DOM-class flags remain concerns even if a live call looks fixed on one target.
