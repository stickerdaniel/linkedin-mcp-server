---
name: triage-reviews
description: Fetch PR review comments, verify each against real code/docs, fix valid issues, commit and push
disable-model-invocation: true
argument-hint: '[PR number]'
---

# Triage PR Review Comments

Fetch all review comments on the current PR, verify each finding against real code, fix valid issues, and push.

## Phase 1: Gather Comments

1. Determine the PR number:
   - Use `$ARGUMENTS` if provided
   - Otherwise: `gh pr view --json number --jq .number`

2. Fetch ALL comments (reviewers post in multiple places):
   ```
   gh api --paginate repos/{owner}/{repo}/pulls/{pr}/reviews
   gh api --paginate repos/{owner}/{repo}/pulls/{pr}/comments
   gh api --paginate repos/{owner}/{repo}/issues/{pr}/comments
   ```

3. Extract unique findings — deduplicate across Copilot, Greptile, and human reviewers. Group by file and line.

## Phase 2: Verify Each Finding

For EVERY finding, verify against real code before accepting or rejecting:

1. **Read the actual code** at the referenced file:line
2. **Check if the issue still exists** — it may already be fixed in a later commit
3. **Verify correctness** using:
   - Code analysis (read surrounding context, trace call paths)
   - Run `btca resources` to see what's available, then `btca ask -r <resource> -q "..."` for library/framework questions
   - Web search for API behavior, language semantics, or CVEs
4. **Classify** each finding:
   - **Valid** — real bug, real gap, or real improvement needed
   - **False positive** — reviewer misread the code, outdated reference, or style preference

## Phase 3: Fix & Ship

1. Fix all **Valid** findings
2. Run the project's lint/test commands (check CLAUDE.md for exact commands)
   - If lint/tests fail, fix the failures before committing
   - If a failure cannot be fixed automatically, skip that fix and report it as **Valid (unfixed)** in the Phase 4 table
3. `git add` only changed files, `git commit` with message:
   ```
   fix: Address PR review feedback

   - <one-line summary per fix>
   ```
4. Push: `gt submit` (or `git push` if not using Graphite)

## Phase 4: Report

Present a final summary table of ALL findings with verdicts:

| # | Source | File:Line | Finding | Verdict | Reason |
|---|--------|-----------|---------|---------|--------|

## Notes

- Never dismiss a finding without reading the actual code first
- If unsure, err toward "Valid" — it's cheaper to fix than to miss a bug
- For library/API questions, always use btca or web search — don't guess
