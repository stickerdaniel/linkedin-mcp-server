---
name: 1-triage-issues
description: Scan all open issues and PRs in stickerdaniel/linkedin-mcp-server and rank them by urgency (severity, user impact, age, references), implementation quality (for PRs — mergeability, CI, diff scope, locale-independence, test coverage), and contributor track record (prior merged PRs, review quality, response cadence). Use when the user asks "what should I tackle first as core maintainer", "which PRs are production-ready", "triage backlog", "scan open issues", or any maintainer-prioritisation question about this repo. Outputs a ranked list with rationale per item, not a fix.
argument-hint: '[label-or-keyword-filter]'
---

# Triage Open Issues and PRs

Goal: in one pass, turn the live state of `stickerdaniel/linkedin-mcp-server` into a maintainer-grade priority list. Read-only. No checkout, no edits, no reproduction. Reproduction belongs in `/2-repro-issue`; PR verification in `/3-verify-pr-fix`.

## Inputs

`$ARGUMENTS` is optional. If set, treat as a label name (`bug`, `enhancement`) or freetext filter to narrow the scope. Otherwise scan everything open.

## Phase 1 — Gather

```bash
REPO=stickerdaniel/linkedin-mcp-server

# Issues  (gh calls the reactions field `reactionGroups`, not `reactions`)
gh issue list --repo $REPO --state open --limit 200 \
  --json number,title,labels,createdAt,updatedAt,author,comments,reactionGroups,body

# PRs
gh pr list --repo $REPO --state open --limit 100 \
  --json number,title,labels,createdAt,updatedAt,author,mergeable,mergeStateStatus,statusCheckRollup,additions,deletions,changedFiles,reviewDecision,isDraft,headRepositoryOwner

# Cross-link issues ↔ PRs via body references
gh search prs --repo $REPO --state open --json number,body,title --limit 100
```

Build a map `issue_number → [referencing_pr_numbers]` by scanning PR bodies for `Closes #`, `Fixes #`, `Resolves #`, plain `#N`.

If `$ARGUMENTS` is given, filter both lists down before scoring.

## Phase 2 — Score each issue

For every open issue, compute four signals. Cite the evidence inline so the user can audit.

- **Severity** (1–5): `5` = data loss / broken-for-all-users / security; `4` = core tool unavailable (e.g. `get_person_profile` returning empty); `3` = degraded output for a subset (locale, edge-case profile); `2` = annoyance / cosmetic; `1` = question / docs. Read the body + labels (`bug`, `critical`, `security`, `regression`).
- **Reach** (1–5): comment count, distinct commenters, `+1`/`👍` reactions, references from other issues.
- **Age vs activity**: `createdAt` → days old; `updatedAt` → days since last activity. Stale-but-high-severity ranks higher than fresh-but-cosmetic.
- **Has fix in flight**: did anyone open a PR (look up in cross-link map)? Is that PR ready to merge?

## Phase 3 — Score each PR

For every open PR, on top of its linked-issue score:

- **Mergeability**: `mergeable: MERGEABLE` + `mergeStateStatus: CLEAN` + `statusCheckRollup` all green → ✓. Otherwise note what blocks (CI red, conflicts, requested changes, draft).
- **Scope**: `additions + deletions` and `changedFiles`. Flag scope creep — does it touch unrelated files? Cross-check `gh pr diff <N> --name-only`.
- **Locale + DOM safety audit**: do `gh pr diff <N> | grep -E "['\"](Connect|Follow|Message|Pending|1st|2nd|3rd)['\"]"` (matches both Python-style `'Connect'` and JS/Go-style `"Connect"`). Any string match on locale-dependent button text is a red flag per `CLAUDE.md → Scraping Rules → detection must be locale-independent`. Also flag class-name selectors (`.entity-result__item`), minimal generic selectors only.
- **One-section-one-navigation**: if the PR touches `PERSON_SECTIONS` / `COMPANY_SECTIONS` in `scraping/fields.py`, check that each entry still maps to exactly one URL.
- **Test coverage**: does the diff add a test in `tests/test_scraping.py`? Mandatory for new tool surfaces, strongly preferred for bug fixes.
- **Contributor audit**:
  ```bash
  gh search prs --repo $REPO --author <login> --state merged --json number,createdAt,mergedAt,additions,deletions --limit 20
  gh search prs --repo $REPO --author <login> --state closed --json number,closedAt,state --limit 10
  ```
  Compute: prior merged PRs in this repo, average time-to-merge, ratio of merged-vs-closed-unmerged. First-time contributors are not penalised — but a contributor whose previous PRs were all closed-unmerged with maintainer pushback is a yellow flag, especially on a large diff. Cite specific PR numbers in the rationale.

For very large or architecturally-loaded PRs (changes to `scraping/extractor.py`, `client/`, or session/auth code), spawn an `Explore` subagent to deep-dive how the PR integrates with the codebase and report integration risk. Use this sparingly — only for PRs over ~200 LOC or PRs touching core paths.

## Phase 4 — Rank and report

Two ranked tables, plus a short verdict. Imperative, no fluff.

```
## Issues — top 10

| # | Issue | Severity | Reach | Age | PR? | Why this rank |
|---|-------|----------|-------|-----|-----|---------------|
| 1 | #366  | 4        | 8     | 12d | #366 ready | Core tool broken, contributor inactive |
| 2 | #389  | 3        | 2     |  5d | none | Pydantic version regression, blocks installs |
| ...

## PRs — production-readiness ranking

| # | PR | Linked issue | Mergeable | Scope | Locale-safe | Tests | Contributor | Verdict |
|---|----|--------------|-----------|-------|-------------|-------|-------------|---------|
| 1 | #386 | #385       | ✓         | +120/-40, 3 files | ✓ | ✓ | 5 prior merged | Ready — merge |
| 2 | #366 | #365       | ✗ conflict | +800/-200, 12 files | ✗ uses "Connect" text | ✗ | 0 prior, 2 closed | Needs maintainer rewrite |
| ...

## Recommended order this week

1. Merge #386 — clean fix, ready.
2. Take over #366 via maintainer-edits — issue is core but PR has locale-issues and stalled contributor.
3. Investigate #389 — needs reproduction first (→ `/2-repro-issue 389`).
```

End with one paragraph naming the *next concrete action* the maintainer should take, and which downstream skill applies (`/2-repro-issue <N>` to confirm bug, `/3-verify-pr-fix <N>` to test the candidate fix).

## Non-negotiables

- Read-only. Do not check out, do not start the MCP server, do not run scrapers. Those are downstream skills.
- Cite evidence inline (PR/issue numbers, file paths, contributor PR history). Never assert "looks risky" without a referenceable signal.
- Locale-dependence and DOM-class-selector usage are hard fails — flag them red regardless of how nice the PR otherwise looks.
- Never recommend "merge as-is" for a PR that hasn't passed the locale/test/scope checks, even if the contributor is well-known.
