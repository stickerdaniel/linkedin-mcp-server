---
name: 3-verify-pr-fix
description: Check out a candidate PR locally, restart the MCP server on the PR branch, re-run the exact same MCP tool call that failed in /2-repro-issue, diff the outputs, and audit the fix for locale-independence, DOM-stability, and scope per CLAUDE.md scraping rules. Use when the user says "verify PR #N", "does this PR fix #M", "check #N locally", "test the fix in #N", or asks whether a candidate PR actually solves a previously-reproduced issue. Assumes /2-repro-issue has already captured the on-main baseline at /tmp/repro-issue-<linked>-main.json — if not, run /2-repro-issue first.
argument-hint: '<pr-number-or-url>'
---

# Verify a Candidate PR Actually Fixes the Issue

Goal: take a PR number, check it out cleanly, re-run the same tool call that failed on `main`, compare the outputs, and produce a verdict — "fixes", "fixes but with concerns", "does not fix", or "cannot run". Pair with `/2-repro-issue` (which produces the baseline). No edits, no merges, no pushes.

## Phase 1 — Resolve the PR and its linked issue

```bash
# Accept "498", "#498", or "https://github.com/.../pull/498" — extract the digits only
PR=$(echo "$ARGUMENTS" | sed -E 's|.*/||; s|#||g' | grep -oE '^[0-9]+' | head -1)
[ -z "$PR" ] && { echo "Invalid input: '$ARGUMENTS'. Pass a PR number or URL." >&2; exit 1; }
REPO=stickerdaniel/linkedin-mcp-server

gh pr view $PR --repo $REPO --json title,body,baseRefName,headRefName,headRepositoryOwner,mergeable,mergeStateStatus,additions,deletions,changedFiles,maintainerCanModify,statusCheckRollup
```

Extract the **linked issue number** from the PR body (`Closes #`, `Fixes #`, `Resolves #`, or plain `#N`). Call it `ISSUE`. If multiple, ask the user which one is the verification target.

## Phase 2 — Pick up the baseline + request metadata

`/2-repro-issue` writes two files for each issue:

- `/tmp/repro-issue-<ISSUE>-main.json` — the on-main response baseline
- `/tmp/repro-issue-<ISSUE>-meta.json` — the `{tool, arguments}` used to call it

Both must exist. The meta file is how this skill replays the *exact* same call instead of guessing tool and args from the response body.

```bash
ls -la /tmp/repro-issue-$ISSUE-main.json /tmp/repro-issue-$ISSUE-meta.json 2>&1
# Both files must exist AND be non-empty. ls alone never fails the script, so
# guard each path before we touch the PR branch or LinkedIn.
[ -s /tmp/repro-issue-$ISSUE-main.json ] || { echo "Missing or empty response baseline. Re-run /2-repro-issue $ISSUE." >&2; exit 1; }
[ -s /tmp/repro-issue-$ISSUE-meta.json ] || { echo "Missing or empty meta file. Re-run /2-repro-issue $ISSUE." >&2; exit 1; }
TOOL=$(jq -r .tool /tmp/repro-issue-$ISSUE-meta.json)
ARGS_JSON=$(jq -c .arguments /tmp/repro-issue-$ISSUE-meta.json)
echo "Replaying: $TOOL($ARGS_JSON)"
```

If either file is missing, stop and tell the user: *"No baseline for #$ISSUE. Run `/2-repro-issue $ISSUE` first so we have an on-main reference to diff against."* Do not silently re-run the reproduction, `/2-repro-issue` is the canonical source of "what's the call, what's the failure mode".

If the baseline is older than 24 h, warn, LinkedIn DOM/data may have shifted. Offer to re-run `/2-repro-issue` first.

## Phase 3 — Check out the PR

```bash
git status --porcelain | head -5     # must be clean
git stash list | head -3             # warn if there are stashes the user may forget

# Remember where to return. `--abbrev-ref HEAD` returns the literal string "HEAD"
# in detached state (CI worktrees, prior PR checkouts), so fall back to the SHA.
CURRENT_REF=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)

# Fetch the PR head into FETCH_HEAD only and check it out as a detached HEAD.
# Skipping a named local branch avoids clobbering a maintainer's existing
# `pr-$PR` work and removes the cleanup-time `git branch -D` foot-gun.
# Guard the fetch: without it a network/auth failure or deleted pull ref would
# leave a stale FETCH_HEAD and the verdict would run against the wrong commit.
git fetch origin "pull/$PR/head" || { echo "git fetch for PR #$PR failed, aborting before checkout." >&2; exit 1; }
PR_SHA=$(git rev-parse FETCH_HEAD)
git checkout --detach "$PR_SHA"

# Any early exit after this point must still return to $CURRENT_REF + clear
# /tmp scratch files. Install a single trap so the fail-fast guards in Phase 4
# do not strand the user on the PR commit.
cleanup_verify() {
  rc=$?
  trap - EXIT INT TERM
  kill $SERVER_PID 2>/dev/null
  wait $SERVER_PID 2>/dev/null
  git checkout "$CURRENT_REF" 2>/dev/null
  rm -f /tmp/verify-pr-$PR.json /tmp/verify-pr-$PR-headers /tmp/verify-pr-$PR.log /tmp/pr-$PR-meta.json
  exit $rc
}
trap cleanup_verify EXIT INT TERM

# Scope sanity check
gh pr view $PR --repo $REPO --json files --jq '.files[].path' | head -20
git diff --stat $(git merge-base "$PR_SHA" origin/main)..$PR_SHA | tail -10
```

If the PR has merge conflicts with `main` (`mergeStateStatus: DIRTY`), continue anyway — local checkout still works — but note it in the verdict.

If `maintainerCanModify: true`, mention that to the user; it unlocks the "take over and push a small follow-up commit" path later.

## Phase 4 — Re-run the same MCP call on the PR branch

`$TOOL` and `$ARGS_JSON` came from the meta file in Phase 2. Restart the server because old workers hold the old code. Probe a free port (default 8000 is commonly taken):

```bash
PORT=8765
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; do PORT=$((PORT+1)); done
uv run -m linkedin_mcp_server --transport streamable-http --port $PORT --log-level INFO > /tmp/verify-pr-$PR.log 2>&1 &
SERVER_PID=$!

# Wait for the port to actually start LISTENing (cap 30s) — a blind sleep would
# let a startup crash silently become a "does not fix" verdict.
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

# notifications/initialized often replies with a pydantic validation error.
# Harmless, same session ID still works for tools/call.
curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"notifications/initialized","params":{}}' > /dev/null

# Same tool + args as baseline, pulled verbatim from /tmp/repro-issue-$ISSUE-meta.json:
curl -s -X POST http://127.0.0.1:$PORT/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/call\",\"params\":{\"name\":\"$TOOL\",\"arguments\":$ARGS_JSON}}" \
  | tee /tmp/verify-pr-$PR.json | head -200

kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null
```

## Phase 5 — Diff and classify

```bash
diff -u /tmp/repro-issue-$ISSUE-main.json /tmp/verify-pr-$PR.json | head -200
```

Classify the PR outcome:

- **Fixes cleanly ✓** — the failing field/section/error from the baseline is gone, no new failures visible, output structure matches `CLAUDE.md → Tool Return Format` (`{url, sections, ...}`).
- **Fixes with concerns ⚠** — works on the issue's target but: (a) introduces new keys/sections not in the documented return format, (b) fails on a second target (different locale, edge case), (c) regresses an unrelated tool, (d) scope creep into unrelated files.
- **Does not fix ✗** — same failure mode persists.
- **Cannot run** — merge conflicts that affect runtime, dependency drift (e.g. PR pins a Python version we don't have), or unrelated breakage.
- **Cannot verify fix (skill limitation)** — `/2-repro-issue` produced a success-baseline, not a failure-baseline (the bug only manifests under crash conditions, stdio transport, specific environments, or was already shipped-fixed). The diff therefore cannot show "before/after fix". Fall back to: (a) **regression check** — same baseline call must still succeed on PR branch, (b) **code audit** against the rules in Phase 6. Report this explicitly as a skill limitation, not as a PR weakness.

For locale-sensitive bugs, run **one extra call** against a deliberately non-English target before declaring "fixes cleanly". The detection logic must not regress on a German/RTL profile.

## Phase 6 — Audit the diff against CLAUDE.md scraping rules

Read the actual diff, not just the file list:

```bash
git diff $(git merge-base "$PR_SHA" origin/main)..$PR_SHA
```

Hard flags (any one = downgrade from ✓ to ⚠):

- **Locale-dependent detection**: any `== "Connect"`, `in ["Pending", "Follow"]`, `contains("1st")`, `aria-label="..."` with translated text. The verb is locale-dependent; attribute *presence* is not. See `CLAUDE.md → detection must be locale-independent`.
- **LinkedIn-class-name selectors**: `.entity-result__item`, `.artdeco-button__text`, etc. Only minimal generic selectors are acceptable (`a[href*="/jobs/view/"]`).
- **Multiple navigations behind one section**: any new entry in `PERSON_SECTIONS` / `COMPANY_SECTIONS` (`scraping/fields.py`) must map to exactly one URL.
- **Missing tests**: bug fixes should add or update a test in `tests/test_scraping.py`. Pure-DOM fixes without test coverage are a yellow flag.

## Phase 7 — Report

```
**PR #<PR>** (fixes #<ISSUE>) — <one-line PR title>
**Mergeable:** <CLEAN | DIRTY conflicts | BLOCKED — reason>
**Scope:** <+X/-Y, N files; flag if unrelated>
**Verdict:** <Fixes ✓ | Fixes with concerns ⚠ | Does not fix ✗ | Cannot run | Cannot verify fix (skill limitation)>
**Evidence:** 2–4 lines of the diff between baseline and PR-branch output that prove the verdict.
**Audit flags:** <locale-dependent | DOM-class selectors | section-mapping violation | missing tests | none>
**Cleaner alternative:** <other PR # | refactor sketch | none — PR is good as-is>
**Recommended next step:** <merge | request changes citing flag X | take over via maintainer-edits (maintainerCanModify=true) | close as not-needed>
```

Then one short paragraph: what the PR actually changes, why it does/doesn't address root cause, and what the locale-independent / DOM-minimal version would look like if the verdict is ⚠ or ✗.

## Phase 8 — Cleanup

```bash
# The EXIT trap installed in Phase 3 runs automatically — it returns to
# $CURRENT_REF and cleans up /tmp/verify-pr-$PR.* on any exit (success, error,
# Ctrl-C). No local branch was ever created (Phase 3 uses a detached HEAD on
# FETCH_HEAD), so nothing needs deleting either.
# Keep /tmp/repro-issue-$ISSUE-main.json + meta.json so the user can re-verify against another PR later.
```

Leave the user on the branch they started on, with a clean working tree.

## Non-negotiables

- Real LinkedIn, same profile, same call as the baseline. Anything else isn't a verification.
- Restart the server after `git checkout` — running workers hold stale code.
- Do not push, do not edit the PR, do not merge. This skill verifies and reports.
- Locale and DOM-class flags are non-overridable: if they're present, the verdict is ⚠ at best, regardless of whether the diff "works on the target".
- If the baseline never captured a failure (e.g. crash-only bug, stdio-only bug, env-specific bug), name it **"Cannot verify fix (skill limitation)"** and lean on code-audit + regression check. Never escalate a missing failure into a PR criticism.
