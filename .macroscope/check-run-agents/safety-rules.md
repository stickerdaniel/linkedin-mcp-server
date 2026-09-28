---
title: Profile and Browser Safety
model: gpt-6-sol
reasoning: xhigh
input: incremental
include:
  - "linkedin_mcp_server/**/*.py"
  - "manifest.json"
requires:
  - lint-and-check
maxBudgetPerRun: 3
maxBudgetPerPR: 15
conclusion: neutral
---

# Profile and browser safety review

Enforce the sections **Browser Identity Rules**, **Profile Safety Rules**, and **Extension Bundle Rules** of the repository guide below on the changed lines. The other sections are background.

@/AGENTS.md

## Tracing

A rule is judged on the code behind a changed line, not the line alone:

- For a destructive filesystem call, trace its path argument back to its origin. A directory the code created itself is outside the rules; a path derived from `USER_DATA_DIR` or another user-supplied root is inside them.
- For an ownership guard, find where it runs relative to the configured source root and to every early return before it.
- For a launch or proxy change, list every egress path the change touches.
- For a `manifest.json` placeholder, note whether it sits inside a string or as a whole `args` element.

The check is done when every rule in the three sections has been applied to every changed hunk in scope. Findings come from changed lines; older code in the same file is background.

## Reporting

Post each violation as an inline comment on the smallest relevant range: the rule, the path or value flow that breaks it, and the fix the guide prescribes.

When there are no findings, make the entire final response exactly `All clear` on one line with nothing else.
