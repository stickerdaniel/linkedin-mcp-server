---
name: 4-address-pr-review
description: Fix a failed CI run and/or Greptile/reviewer comments on an already-open PR against stickerdaniel/linkedin-mcp-server, using the repo's own coding rules (CLAUDE.md) as the checklist, then commit and push the fixes back to the same PR branch. Use when the user says "the build failed on the PR", "check the review comments and update", "address the feedback on #N", or similar. Assumes the PR already exists (see checkpoint/history for how it was opened) and the local clone still has the `fork` remote configured.
argument-hint: '<pr-number>'
---

# Address CI Failures and Review Comments on an Open PR

Goal: diagnose why CI is red and/or what reviewers flagged, fix root causes (not just symptoms), verify locally, and push an update to the same PR branch — without scope creep into unrelated files.

## Phase 1 — Pull CI status and review comments

```bash
PR=$ARGUMENTS
REPO=stickerdaniel/linkedin-mcp-server

gh pr view $PR --repo $REPO --json statusCheckRollup -q '.statusCheckRollup[] | {name,status,conclusion}'
gh api repos/$REPO/pulls/$PR/comments --paginate > /tmp/pr$PR-comments.json   # inline review comments
gh api repos/$REPO/issues/$PR/comments --paginate > /tmp/pr$PR-issue-comments.json  # PR-level comments (e.g. Greptile summary)
```

For a FAILURE conclusion, get the failing job's log:

```bash
RUN_ID=$(gh run list --repo $REPO --branch <head-branch> --json databaseId,conclusion -q '.[] | select(.conclusion=="failure") | .databaseId' | head -1)
gh run view $RUN_ID --repo $REPO --log-failed > /tmp/pr$PR-fail.log   # can be large; grep it, don't dump it
```

Parse both comment JSON files (Python one-liner is fine) to extract `{path, line, body}` for each inline comment — that's the actual review surface to address, not just the summary text.

## Phase 2 — Known recurring CI trap: hardcoded tool counts

`tests/test_daemon_election.py::TestRealOwner::test_a_proxy_serves_the_real_owners_tools_over_loopback` asserts an exact `len(names) == N` for the total MCP tools exposed via the daemon-proxy. **Any PR that adds a new top-level `@mcp.tool` registration will fail this test** until the number is bumped. Grep for it first before deep-diving a mysterious CI failure:

```bash
grep -n "len(names) ==" tests/test_daemon_election.py
```

If failing for this reason, bump the count by the number of new tools added and move on — this isn't a real regression, just a magic number that has to track the tool count.

## Phase 3 — Fix DOM-selector review flags against CLAUDE.md

Greptile (and human reviewers) will flag any new scraping code that violates the repo's own rules in `CLAUDE.md → Scraping Rules`:

- **Never select by LinkedIn's own CSS class names** (e.g. `button.share-actions__primary-action`). These are auto-generated/unstable and explicitly forbidden.
- **Never gate detection logic on locale-dependent text** (`"Post to Anyone"`, `"Connect"`, etc.) — prefer URL patterns, attribute *presence* (not value), or structural position/counts.
- Preferred replacement pattern discovered while building `create_post`: scope a selector to the enclosing dialog/section, then pick out the *one or two* buttons that lack an attribute every other button in that scope carries (e.g. `button:not([aria-label])` isolated the composer's author-switch and Post-submit buttons because every other composer button has an `aria-label`). Verify this kind of selector **live** before trusting it — write a small throwaway probe script (Playwright/Patchright, using the existing authenticated persistent profile at `~/.linkedin-mcp/profile`), dump `element.attributes` for the candidates, confirm uniqueness, then delete the probe script. Do not guess at selectors from memory of a prior session — the DOM may have changed.

## Phase 4 — Fix substring/ambiguous-matching flags

Any code matching a caller-supplied name against on-page text (e.g. "post as this Page") must use **exact equality** (after casefold + strip), never `in`/substring containment. Substring matching lets a short/generic name silently match the wrong option (e.g. `"Labs"` matching "Peacock Labs"), or falsely early-exit thinking the current state already matches. When multiple options can exactly match, **fail loudly and refuse** rather than picking the first one — silent wrong-identity actions (e.g. posting under the wrong company Page) are worse than a refusal.

If the on-page label carries extra lines/text beyond the actual name (e.g. `"<Name>\nPost to Anyone"`), match only the relevant line/segment, not the full string — verify the exact shape live via the same probe-script approach as Phase 3.

## Phase 5 — Add missing test coverage

Reviewers will flag any new write/destructive tool with zero test coverage. Follow existing conventions in `tests/test_tools.py` (`TestPostTools`, `_make_mock_extractor`, `get_tool_fn` helper) and `tests/test_scraping.py` for extractor-level unit tests. Minimum coverage for a new write tool:

- Tool registration + dry-run gating (confirm flag False vs True forwarded correctly)
- Refusal path (e.g. ambiguous/absent match) passes through untouched
- The tool's name added to `TestToolTimeouts`' enumerated tuples (both custom-timeout and default-timeout tests) — easy to miss, causes silent gaps in timeout coverage.
- Any new pure-function helper (e.g. an exact-match comparator) gets its own small `TestXxx` class with a regression test for the specific bug it fixes.

## Phase 6 — Verify before pushing

```bash
uv run ruff check <changed files>
uv run ruff format <changed files>       # then re-run tests if this changed anything
uv run ty check <changed files>
uv run pytest tests/test_scraping.py tests/test_tools.py -q     # fast, targeted
uv run pytest tests/test_daemon_election.py -q                  # if you touched the tool-count assertion
```

Note: on Windows, `tests/test_daemon_election.py::TestRealOwner::*` (subprocess-based real-daemon tests) fail locally with `AttributeError: module 'signal' has no attribute 'SIGKILL'` — this is a pre-existing POSIX-only limitation, not something your change broke. Confirm by checking the same failures occur on a clean `git stash` before your changes. Don't try to fix it; it doesn't run in this form on Windows and CI runs it on Linux/macOS anyway.

Also guard against `uv sync`/`uv run` silently rewriting `uv.lock` with unrelated platform-diff churn — `git checkout -- uv.lock` before committing if it shows as modified and you didn't intend to touch dependencies.

## Phase 7 — Commit and push

```bash
git add <only the files that changed for this fix>
git commit -m "Address review feedback on <feature> PR

- <bullet per fix, referencing the specific review comment/CI failure addressed>"
git push fork <head-branch>
```

Then optionally re-check `gh pr checks $PR --repo $REPO` to confirm CI goes green.

## Non-negotiables

- Fix root causes flagged by reviewers, not just whatever makes the diff smaller. A P1 (misrouting/wrong-identity risk) always gets fixed even if it requires restructuring the matching logic, not just tightening a regex.
- Verify any new DOM selector live against the real site before trusting it — this repo's browser-automation code has no room for "looks right" without confirmation, per its own contribution rules.
- Never widen scope beyond what reviewers/CI flagged — no drive-by refactors of unrelated code in the same PR.
