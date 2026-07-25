---
name: triage-reviews
description: Fetch PR review comments, verify each against real code/docs, fix valid issues, commit and push
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

## Phase 3.5: Reply on each thread, then resolve it

Pushing the fix isn't enough. Each inline review comment lives in its own thread that GitHub keeps showing as "Unresolved" until someone explicitly resolves it. The skill must close that loop for every Valid and False-positive finding so the PR view actually reflects what was triaged.

Workflow per finding:

1. **Reply on the thread** with the verdict and evidence:
   - Valid + fixed: `Fixed in <short-sha>. <one-line what changed>.`
   - Valid + unfixed: `Valid, deferred to follow-up. Reason: <why>.`
   - False positive: `False positive. <one-line evidence: file:line shows X, or doc link Y>.`

2. **Resolve the thread only when the finding is Fixed or False positive.** `Valid + unfixed` threads must stay open so the PR view continues to surface the real bug. Resolving them would let the PR look ready while the bug is still in the code. Leave the maintainer to close those threads when they file the follow-up.

GitHub's review threads can only be resolved via GraphQL (REST has no endpoint). The thread ID is a GraphQL node ID, not the REST comment ID, so fetch both together. Use `--paginate` so PRs with more than 100 threads are covered, the page size cap is per-request not total:

```bash
PR=<pr-number>
OWNER=<owner>
REPO=<repo>

gh api graphql --paginate -f query='
  query($owner: String!, $repo: String!, $pr: Int!, $endCursor: String) {
    repository(owner: $owner, name: $repo) {
      pullRequest(number: $pr) {
        reviewThreads(first: 100, after: $endCursor) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            isResolved
            comments(first: 1) { nodes { databaseId path line body } }
          }
        }
      }
    }
  }' \
  -F owner="$OWNER" -F repo="$REPO" -F pr=$PR \
  --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | {threadId: .id, commentId: .comments.nodes[0].databaseId, path: .comments.nodes[0].path, line: .comments.nodes[0].line}' \
  > /tmp/triage-threads-$PR.json
```

Each entry now has `{threadId, commentId, path, line}`. Match it against your Phase-2 finding map (by `path` + `line` or `commentId`). For each match:

```bash
# Post reply (REST endpoint for replies on a specific review comment)
gh api -X POST "/repos/$OWNER/$REPO/pulls/$PR/comments/$COMMENT_ID/replies" \
  -f body="Fixed in $SHORT_SHA. <one-line>."

# Resolve the thread (GraphQL) — Fixed and False-positive only. SKIP for Valid+unfixed.
gh api graphql -f query="
  mutation {
    resolveReviewThread(input: {threadId: \"$THREAD_ID\"}) {
      thread { isResolved }
    }
  }"
```

If the parent review left a separate top-level summary comment (Greptile's `Greptile Summary` issue-level comment, for example), leave it alone, only inline review threads need resolving.

Cap: resolve only threads tied to findings you actually classified. Do not bulk-resolve unrelated threads (other reviewers, human discussion, follow-up questions that aren't from this triage round).

## Phase 4: Report

Present a final summary table of ALL findings with verdicts:

| # | Source | File:Line | Finding | Verdict | Reason | Thread |
|---|--------|-----------|---------|---------|--------|--------|

Last column: `replied + resolved`, `replied + still open` (e.g. waiting on reviewer), or `n/a` (no inline thread, only summary). If any thread stayed open, name it explicitly so the next pass picks it up.

## Notes

- Never dismiss a finding without reading the actual code first
- If unsure, err toward "Valid" — it's cheaper to fix than to miss a bug
- For library/API questions, always use btca or web search — don't guess
