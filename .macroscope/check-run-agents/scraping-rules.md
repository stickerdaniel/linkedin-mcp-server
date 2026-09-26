---
title: Scraping Rules
model: gpt-6-sol
reasoning: xhigh
input: incremental
include:
  - "linkedin_mcp_server/scraping/**"
  - "linkedin_mcp_server/tools/**"
  - "linkedin_mcp_server/core/**"
requires:
  - lint-and-check
maxBudgetPerRun: 3
maxBudgetPerPR: 15
conclusion: neutral
---

# Scraping rules review

Enforce the sections **Scraping Rules** and **Tool Return Format** of the repository guide below on the changed lines. The other sections are background.

@/AGENTS.md

## Tracing

A rule is judged on the code behind a changed line, not the line alone:

- For a changed entry in `PERSON_SECTIONS` or `COMPANY_SECTIONS`, follow it to the code that navigates for it and count the URLs it visits.
- Read JavaScript strings passed to `page.evaluate` as selectors and classification logic too.
- For a changed classification, name what the decision reads: a URL, an attribute's presence, a count, or text.

The check is done when every rule in both sections has been applied to every changed hunk in scope. Findings come from changed lines; older code in the same file is background.

## Reporting

Post each violation as an inline comment on the smallest relevant range: the rule, the offending expression, and the replacement the guide prescribes.

When there are no findings, make the entire final response exactly `All clear` on one line with nothing else.
