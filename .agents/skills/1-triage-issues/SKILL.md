---
name: 1-triage-issues
description: Triage backlog, scan open issues, rank what to tackle first, or which PRs are production-ready.
argument-hint: '[label-or-keyword-filter]'
---

# Triage open issues and PRs

Turn the live state of `stickerdaniel/linkedin-mcp-server` into a maintainer priority list. Read-only. No checkout, no labels, no comments, no closure, no reproduction.

Reproduction belongs in `/2-repro-issue`. PR verification belongs in `/3-verify-pr-fix`.

## Inputs

`$ARGUMENTS` is optional. If set, treat as a label name (`bug`, `enhancement`) or freetext filter. Otherwise scan everything open.

## 1. Gather

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

If `$ARGUMENTS` is given, filter both lists before scoring.

Read each issue body and its comments, including linked canonical issues.

## 2. Packet admission

Before severity, age, reach, or contributor ranking, classify every open issue.

| Packet state | Maintainer action |
| --- | --- |
| Material evidence missing | Recommend `needs more info` with the specific missing fields. Exclude it from the reproduction shortlist. Existing labels alone do not prove completeness. |
| Evidence supplied after a request | Read the new evidence. Recommend removing `needs more info` only when it resolves the material gap. A new comment does not automatically clear the label. |
| Same underlying report as a canonical issue | Note unique reviewed evidence for the canonical issue. Recommend a `Duplicate of #N` comment and a separate close, each with an explicit yes. Do not post or close here. |
| Evaluable bug or feature | Continue to scoring. Completeness is not a promise of implementation. |
| Source evidence supports a narrower claim than the reporter's explanation | State that narrower finding. Ask for targeted missing evidence only if the next decision needs it. |

This step is complete when every scanned issue is either evaluable, missing-evidence with a field list, or a named canonical duplicate. Incomplete packets never enter the reproduction shortlist.

## 3. Score each evaluable issue

For every evaluable open issue, compute four signals. Cite the evidence inline.

- **Severity** (1 to 5): `5` = data loss / broken-for-all-users / security; `4` = core tool unavailable (e.g. `get_person_profile` returning empty); `3` = degraded output for a subset (locale, edge-case profile); `2` = annoyance / cosmetic; `1` = question / docs. Read the body + labels (`bug`, `critical`, `security`, `regression`).
- **Reach** (1 to 5): comment count, distinct commenters, `+1` reactions, references from other issues.
- **Age vs activity**: `createdAt` → days old; `updatedAt` → days since last activity. Stale-but-high-severity ranks higher than fresh-but-cosmetic.
- **Has fix in flight**: did anyone open a PR (look up in cross-link map)? Is that PR ready to merge?

## 4. Score each PR

For every open PR, on top of its linked-issue score:

- **Mergeability**: `mergeable: MERGEABLE` + `mergeStateStatus: CLEAN` + `statusCheckRollup` all green → ✓. Otherwise note what blocks (CI red, conflicts, requested changes, draft).
- **Scope**: `additions + deletions` and `changedFiles`. Flag scope creep. Cross-check `gh pr diff <N> --name-only`.
- **Locale + DOM safety audit**: do `gh pr diff <N> | grep -E "['\"](Connect|Follow|Message|Pending|1st|2nd|3rd)['\"]"` (matches both Python-style `'Connect'` and JS/Go-style `"Connect"`). Any string match on locale-dependent button text is a red flag per `CLAUDE.md` scraping rules. Also flag class-name selectors (`.entity-result__item`). Minimal generic selectors only.
- **One-section-one-navigation**: if the PR touches `PERSON_SECTIONS` / `COMPANY_SECTIONS` in `scraping/fields.py`, check that each entry still maps to exactly one URL.
- **Test coverage**: does the diff add coverage beside the canonical owner from `docs/scraping-architecture.md`? Most owners use `tests/scraping/test_<owner>.py`; `fields`, `identifiers`, and `link_metadata` use `tests/test_fields.py`, `tests/test_identifiers.py`, and `tests/test_link_metadata.py`. Facade-only changes belong in `tests/scraping/test_facade_*.py`. Mandatory for new tool surfaces, strongly preferred for bug fixes.
- **Contributor audit**:
  ```bash
  gh search prs --repo $REPO --author <login> --state merged --json number,createdAt,mergedAt,additions,deletions --limit 20
  gh search prs --repo $REPO --author <login> --state closed --json number,closedAt,state --limit 10
  ```
  Compute: prior merged PRs in this repo, average time-to-merge, ratio of merged-vs-closed-unmerged. First-time contributors are not penalised. A contributor whose previous PRs were all closed-unmerged with maintainer pushback is a yellow flag on a large diff. Cite specific PR numbers.

For very large or architecturally-loaded PRs (changes spanning canonical scraping owners or the generated graph in `docs/scraping-architecture.md`, `client/`, or session/auth code), spawn an `Explore` subagent to deep-dive how the PR integrates with the codebase and report integration risk. Treat `scraping/extractor.py` as the thin facade, not the default workflow owner. Use this sparingly: only for PRs over ~200 LOC or PRs touching core paths.

## 5. Rank and report

Two ranked tables, a missing-evidence list, and a short verdict.

```
## Issues: top 10 evaluable

| # | Issue | Severity | Reach | Age | PR? | Why this rank |
|---|-------|----------|-------|-----|-----|---------------|
| 1 | #366  | 4        | 8     | 12d | #366 ready | Core tool broken, contributor inactive |

## Needs more info (not a reproduction shortlist)

| Issue | Missing fields |
|-------|----------------|
| #986  | UI language, region, product, redacted compose capture |

## PRs: production-readiness ranking

| # | PR | Linked issue | Mergeable | Scope | Locale-safe | Tests | Contributor | Verdict |
|---|----|--------------|-----------|-------|-------------|-------|-------------|---------|
| 1 | #386 | #385       | ✓         | +120/-40, 3 files | ✓ | ✓ | 5 prior merged | Ready to merge |

## Recommended order this week

1. Merge #386. Clean fix, ready.
2. Request the missing fields on #986. Do not send it to /2-repro-issue yet.
3. Investigate #389 from packet and source (→ `/2-repro-issue 389`).
```

End with one paragraph naming the next concrete action, and which downstream skill applies. Only evaluable items go to `/2-repro-issue`. `/3-verify-pr-fix` is for candidate PRs.

## Non-negotiables

- Read-only. Do not check out, start the MCP server, run scrapers, apply labels, comment, close, or assign.
- Cite evidence inline (PR/issue numbers, file paths, contributor PR history).
- Locale-dependence and DOM-class-selector usage are hard fails.
- Never recommend "merge as-is" for a PR that has not passed the locale/test/scope checks.
