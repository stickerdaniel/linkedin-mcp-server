---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
title: "fix: guard job-hydration text check with a per-locale table"
plan_type: fix
created: 2026-08-11
origin: https://github.com/stickerdaniel/linkedin-mcp-server/pull/718 (Greptile review comment)
---

# fix: guard job-hydration text check with a per-locale table

## Summary

PR #718 added an `is_job` hydration-wait branch to `_extract_loaded_section`
(`extractor.py:1626-1646`) that polls `main.innerText.includes('About the
job')` before extracting a `/jobs/view/` page, closing the race from issue
#687. Greptile's automated review flagged that this keys on a single literal
English string: on a LinkedIn UI rendered in another language, the check
never matches, the full timeout elapses, and the original race is
unresolved for that locale. A prior review pass already added a comment
documenting this gap (commit f8579cf) but did not fix it.

Fix: replace the single hardcoded string with a small per-locale table of
equivalent headings, checked via "does innerText contain any known-locale
heading" — mirroring an existing, established pattern in this exact file
(`_MESSAGING_CHROME_STRINGS`, `extractor.py:664`) rather than inventing a new
mechanism.

## Problem Frame

`AGENTS.md`'s Scraping Rules state: "Detection must be locale-independent... Where text is genuinely the only signal, guard it behind an explicit per-locale table and document the limitation in code." The current `is_job` branch does neither — it embeds one English string directly in the `wait_for_function` predicate with only a comment noting the gap.

This codebase already has a template for exactly this situation:
`_MESSAGING_CHROME_STRINGS` (`extractor.py:664`) is a `dict[str, _MessagingChromeTable]` keyed by locale, used by `strip_conversation_chrome()`. Its own comment states the browser context locale is forced to `en-US` (`core/browser.py:262`), so the `"en"` entry is the one that actually fires in practice today; other locale entries exist for defense-in-depth and fall through gracefully (unmatched locale keeps original behavior) rather than crashing or hard-failing.

The `is_job` branch differs in one respect: `strip_conversation_chrome` receives an explicit `locale` parameter from its caller (always `"en"` today — never wired to a real per-request locale), whereas the `wait_for_function` JS predicate has no Python-side locale context to select a single table entry. The fix should check the innerText against *all* configured heading variants rather than needing to pick one — cheap, since the table stays small, and consistent with "an unmatched/unknown case degrades gracefully" instead of guessing a locale.

## Requirements

- R1: The hydration-wait predicate must no longer rely on a single hardcoded English string; it must check against an explicit, named per-locale table, per `AGENTS.md`'s Scraping Rules.
- R2: The table must be modeled on the existing `_MESSAGING_CHROME_STRINGS` pattern in the same file (same file, same shape of "small dict + documented limitation"), not a new mechanism.
- R3: The table is best-effort and non-exhaustive — this must be stated in a comment, matching the existing precedent's honesty about being incomplete.
- R4: Existing English-locale behavior and tests must continue to pass unchanged (regression safety).
- R5: At least one additional locale's heading must be added and proven by a new test to now also trigger the wait correctly, demonstrating the original race is closed for that locale too.
- R6: The fix stays scoped to the `is_job` branch added in PR #718 — no changes to `_MESSAGING_CHROME_STRINGS`, `strip_conversation_chrome`, or any other hydration-wait branch (`is_activity`, `is_search`, `is_company_people`, `is_details`).

## Key Technical Decisions

**KTD1: Model the new table as a plain `dict[str, str]` (locale -> heading text), not a dataclass.**
`_MessagingChromeTable` is a dataclass because it groups multiple distinct chrome-boundary strings per locale. The job-hydration case needs exactly one string per locale (the heading), so a flat `dict[str, str]` is the right-sized equivalent — same *pattern* (locale-keyed table, documented limitation, graceful degradation), without copying structure the job case doesn't need. Alternative considered: reusing `_MessagingChromeTable`'s shape — rejected as over-structured for a single string per locale.

**KTD2: Check "innerText contains any of the table's values", not "look up one locale and check that value".**
`strip_conversation_chrome` can look up a single entry because its caller supplies an explicit `locale` parameter (defaulted to `"en"`, never dynamically set today). The `wait_for_function` predicate runs inside the browser with no such parameter — Python has no reliable signal for which locale the current page is actually rendering in (the forced `en-US` context locale is a *browser* setting, not a *guaranteed* observed page-render language). Checking membership against the full set of configured heading strings is cheap at this table's size and avoids needing to plumb a locale value through `_extract_loaded_section` for a single call site. Alternative considered: accepting a `locale` parameter on `_extract_loaded_section` mirroring `strip_conversation_chrome` — rejected as unnecessary plumbing; nothing in the current call chain has a real per-request locale to pass, so the parameter would always be the same hardcoded default in practice.

**KTD3: Cover a small, explicitly best-effort set of locales — not an exhaustive list.**
Per the plan's scope instruction and `AGENTS.md`'s own tone ("guard it behind an explicit per-locale table" — a table, not a claim of completeness), add 2-3 widely-used LinkedIn UI locales beyond English (e.g. German, Spanish, French) as a representative, documented-as-partial set. This mirrors `_MESSAGING_CHROME_STRINGS` currently shipping with only one (`"en"`) entry — the point is the *mechanism* for adding more later, not exhaustive day-one coverage.

## Scope Boundaries

**In scope:**
- Replace the single English string in the `is_job` `wait_for_function` predicate (`extractor.py:1636-1646`) with a lookup against a new small per-locale heading table.
- Add the new table near `_MESSAGING_CHROME_STRINGS` or directly above the `is_job` branch (implementer's call on ordering — cite the existing precedent as the pattern to place it near).
- Update the existing one-line comment (added in f8579cf) to reflect that the limitation is now mitigated (best-effort table) rather than fully open.
- Add test coverage: existing English-path tests continue to pass unmodified where possible; add at least one new test proving a non-English heading now also triggers the wait.

**Out of scope / deferred to follow-up work:**
- Wiring a real per-request locale signal through `_extract_loaded_section` / `scrape_job` (the `strip_conversation_chrome`-style `locale` parameter) — nothing in the current call chain has one to provide; this would be new plumbing beyond the reported issue.
- Exhaustive locale coverage — the table is explicitly best-effort, matching the existing `_MESSAGING_CHROME_STRINGS` precedent (currently one entry).
- Any other hydration-wait branch's text-based checks (`is_details`'s "Load more"/"More profiles for you" checks have the same underlying English-only limitation, per the earlier review comment on this PR) — those are pre-existing and outside this PR's diff.

## Implementation Units

### U1. Replace the hardcoded English heading with a per-locale table lookup

**Goal:** Close the locale gap Greptile flagged, without duplicating or rewriting the surrounding hydration-wait mechanism.

**Requirements:** R1, R2, R3, R4, R5, R6 (KTD1, KTD2, KTD3)

**Dependencies:** none (builds directly on PR #718's already-merged-to-branch `is_job` branch)

**Files:**
- `linkedin_mcp_server/scraping/extractor.py` (modify the `is_job` branch and add the new table)
- `tests/test_scraping.py` (extend existing job-hydration test group)

**Approach:**
1. Add a small module-level table near the `is_job` branch (or beside `_MESSAGING_CHROME_STRINGS`, matching KTD1):
   - `_JOB_DESCRIPTION_HEADINGS: dict[str, str] = {"en": "About the job", ...}` with 2-3 additional locale entries (KTD3). Use the actual LinkedIn-rendered heading text for each added locale (implementer should use best available knowledge of LinkedIn's UI strings for common locales — e.g. German "Über den Job", or whatever the accurate rendered string is; if genuinely uncertain of the exact production string for a given locale, prefer fewer, verifiably-accurate entries over more guessed ones).
   - A comment stating the table is best-effort/non-exhaustive (R3), referencing the same limitation language already used for `_MESSAGING_CHROME_STRINGS`.
2. In the `is_job` `wait_for_function` call, change the JS predicate from a single `.includes('About the job')` check to checking membership against the table's values — pass the table's values into the page function (e.g. as a JS array literal built from `list(_JOB_DESCRIPTION_HEADINGS.values())`) and check `main.innerText` against each with `.some(...)` or equivalent, per KTD2.
3. Update the existing limitation comment (from f8579cf) to describe the mitigation: still text-based and still not exhaustive, but now covers more than one locale via the table, consistent with R3.
4. Do not touch `_MESSAGING_CHROME_STRINGS`, `strip_conversation_chrome`, or any other `_extract_loaded_section` branch.

**Patterns to follow:** `_MESSAGING_CHROME_STRINGS` (`extractor.py:664`) for the locale-table shape and its accompanying documented-limitation comment style; the existing `is_search`/`is_activity` branches for how a `wait_for_function` JS string is constructed and passed.

**Test scenarios:**
- Regression: a job page whose innerText contains the English heading ("About the job") still triggers `wait_for_function` and resolves — covers the existing `test_job_page_waits_for_description_content` case; verify it still passes unmodified (or with only mechanical updates if the predicate string changed shape).
- New locale coverage: a job page whose innerText contains one of the newly-added non-English headings (and does *not* contain the English string) triggers the wait and successfully resolves — proving the race is now also closed for that locale. Covers R5.
- Negative/regression: a non-job URL still does not trigger the wait at all (existing `test_non_job_page_does_not_wait_for_job_content` — unaffected by this change, verify it still passes).
- Timeout still degrades gracefully: a job page whose innerText matches none of the table's headings (e.g. an unlisted locale) times out and falls through to extract available text, same as before (existing `test_job_page_timeout_proceeds_gracefully` — verify unaffected).

**Verification:** All four scenarios above pass; the full existing test suite (`tests/test_scraping.py`, `tests/test_tools.py`) has no regressions; the diff is scoped to the `is_job` branch, its new table, and its tests only.

## Definition of Done

- `is_job` branch checks against a small, named, per-locale heading table instead of one hardcoded English string.
- Table and its limitation are documented in code, matching the existing `_MESSAGING_CHROME_STRINGS` precedent's tone and shape.
- New test proves at least one non-English locale now correctly triggers the hydration wait.
- All pre-existing job-hydration tests (English case, non-job isolation, timeout fallback) still pass.
- Full test suite passes with no regressions.
- No changes outside `extractor.py`'s `is_job` branch/new table and `test_scraping.py`'s job-hydration test group.
