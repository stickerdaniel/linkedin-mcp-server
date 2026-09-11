---
artifact_contract: ce-unified-plan/v1
artifact_readiness: implementation-ready
execution: code
product_contract_source: ce-plan-bootstrap
title: "fix: Wait for job description hydration in get_job_details"
plan_type: fix
created: 2026-08-11
origin: https://github.com/stickerdaniel/linkedin-mcp-server/issues/687
---

# fix: Wait for job description hydration in get_job_details

## Summary

`get_job_details` intermittently returns a `job_posting` section that is
missing the "About the job" description block, even though everything before
and after that block extracted correctly. An immediate retry on the same job
id succeeds. This is a race between LinkedIn's client-side hydration of the
description panel and the extractor's fixed scroll-wait budget, not a
truncation or selector bug. Fix: mirror the existing hydration-wait pattern
(`is_activity`, `is_search`, `is_company_people`, `is_details`) with a new
`is_job` branch that waits for the description content to actually appear in
`<main>` before extraction proceeds.

## Problem Frame

`linkedin_mcp_server/scraping/extractor.py`'s `_extract_loaded_section`
(`extractor.py:1497`) runs a shared post-navigation pipeline for every page
type: wait for `<main>` to exist, dismiss modals, then apply one or more
page-type-specific hydration waits before scrolling and reading
`main.innerText`. Four page types already get a targeted `wait_for_function`
guard because their content hydrates asynchronously after `<main>` first
renders:

- `is_activity` (`/recent-activity/`, company `/posts`) — waits for
  `main.innerText.length > 200`
- `is_search` (`/search/results/`) — waits for `main.innerText.length > 100`
- `is_company_people` (`/company/.../people/`) — waits for an `a[href*="/in/"]`
  anchor to appear
- `is_details` (`/details/...`) — waits for the sidebar placeholder text to be
  replaced, then clicks any "Show more" button

`/jobs/view/<id>/` job pages (`scrape_job`, `extractor.py:3025`) go through
the same `extract_page` → `_extract_loaded_section` path but have no branch
at all. Extraction falls straight through to the generic 5-iteration,
0.5s-pause scroll loop and then reads `innerText` unconditionally. When the
description panel hydrates after that fixed window, the reader captures the
job header, apply/save chrome, and trailing boilerplate, but the description
block that would have sat in between never rendered — producing a
normal-looking result with a silent gap.

This is a distinct root cause from PR #559 (open, unmerged), which adds an
`is_job` branch to click a `button.show-more-less-html__button--more` "See
more" toggle that expands an `overflow-hidden` collapsed description.
Collapsed content is still present in `innerText` regardless of expansion
state, and the issue reporter reproduced the bug with no code change between
a failing and a succeeding call on the same job id — ruling out a
collapse/expand mechanism as the cause. This plan does not touch or duplicate
PR #559's click-to-expand fix; it addresses the separate hydration-timing gap
the issue also names. Both may reasonably land independently.

## Requirements

- R1: `get_job_details` must not return a result missing the "About the job"
  description block when the block would have appeared given more time —
  i.e., extraction must wait for the description content to hydrate rather
  than relying on the fixed scroll budget alone.
- R2: The fix must follow the existing `_extract_loaded_section` branch
  pattern (a URL-path check plus a bounded `wait_for_function`) rather than
  introducing a new waiting mechanism or architecture.
- R3: The wait must be bounded and must not raise on timeout — consistent
  with every existing branch, a timed-out wait logs at debug level and falls
  through to extraction as-is (partial-content-over-exception is the
  established contract for this function).
- R4: The change must not alter behavior for any non-job URL path.

## Key Technical Decisions

**KTD1: Detect hydration by polling for the description text marker, not a CSS class.**
Wait for `main.innerText` to contain the literal string `"About the job"`
(the heading LinkedIn renders directly above the description body, per the
issue's own structural diff) rather than a `.jobs-description__content`-style
selector. Rationale: `is_activity`/`is_search` already use innerText content
heuristics rather than brittle class-name selectors, and PR #559's discussion
shows LinkedIn's job-page DOM/class names change independently of this
concern — polling for the rendered heading text is more resilient and keeps
this branch symmetric with its two closest siblings. Alternative considered:
waiting for a specific description container selector — rejected because it
reintroduces the class-name fragility this codebase already avoids elsewhere
in this function, and because scoping to the same evidence the bug report
used (the "About the job" heading itself) is the most direct way to close
the exact gap described.

**KTD2: Match the `is_search` timeout and log style (10s, debug-level, no raise).**
Reuses the existing constant timeout value and logging convention already
used by `is_activity`/`is_search` rather than introducing a new tunable —
keeps the branch a same-shape addition, not a new pattern.

## Scope Boundaries

**In scope:**
- Add an `is_job` branch to `_extract_loaded_section` that waits for the
  "About the job" text marker to appear in `<main>` before the scroll/extract
  step runs, for `/jobs/view/` URLs.
- Add test coverage for the new branch (waits when on a job URL, does not
  wait on non-job URLs, proceeds gracefully on timeout).

**Out of scope / deferred to follow-up work:**
- PR #559's "See more" collapsed-description click fix — separate mechanism,
  separate PR, not duplicated here.
- The issue's secondary suggestion to surface an explicit `section_errors`
  entry when the description never appears even after the wait — the issue
  frames this as a "relatedly" nice-to-have, not the core defect, and adding
  it would require a way to distinguish "genuinely no description" (some job
  postings are terse) from "extraction incomplete," which is a separate
  design question from the hydration-timing race this plan closes.

## Implementation Units

### U1. Add `is_job` hydration-wait branch to `_extract_loaded_section`

**Goal:** Wait for the job description to hydrate before extraction, closing
the race described in issue #687.

**Requirements:** R1, R2, R3, R4 (KTD1, KTD2)

**Dependencies:** none

**Files:**
- `linkedin_mcp_server/scraping/extractor.py` (modify `_extract_loaded_section`)

**Approach:**
1. In `_extract_loaded_section`, after the existing `is_details` block and
   before the "Scroll to trigger lazy loading" section (`extractor.py`
   around line 1626), add:
   - `is_job = "/jobs/view/" in path` (reuse the `path` variable already
     computed at the top of the function for `is_activity`, matching the
     parsed-path convention so a query string on the URL is handled
     correctly, same as `is_activity`).
   - A `wait_for_function` call polling
     `main.innerText.includes('About the job')`, wrapped in the same
     `try`/`except PlaywrightTimeoutError` + `logger.debug(...)` shape used
     by `is_activity`/`is_search`, with a 10000ms timeout (KTD2).
2. Do not add a "Show more" click loop for `is_job` — that belongs to PR
   #559's separate fix, not this branch.
3. Leave every other branch (`is_activity`, `is_search`, `is_company_people`,
   `is_details`) and the scroll loop below unchanged.

**Patterns to follow:** the `is_search` branch immediately above (same
`wait_for_function` + timeout + debug-log-on-timeout shape); `is_activity`
for the `path`-based URL match convention.

**Test scenarios:**
- Job URL (`/jobs/view/12345/`) triggers `wait_for_function` exactly once
  (mirrors `test_search_results_page_waits_for_content`).
- Non-job URL (e.g. a plain profile URL) does not trigger the job wait
  (mirrors `test_non_search_page_does_not_wait_for_search_content`).
- Job URL where the wait times out (`PlaywrightTimeoutError` raised by the
  mocked `wait_for_function`) still returns extracted text instead of
  raising — extraction proceeds gracefully (mirrors
  `test_search_results_timeout_proceeds_gracefully`).
- `scrape_job` end-to-end: with `_extract_loaded_section`'s new branch in
  place and a mocked page whose `innerText` includes "About the job" text,
  `scrape_job` returns a `job_posting` section containing that text (covers
  the reported symptom directly — the description is present in the
  result).

**Verification:** All new and existing tests in `tests/test_scraping.py`
pass; the new `is_job` branch behaves identically in shape to `is_search`
when compared side-by-side in the diff.

## Test File

- `tests/test_scraping.py` — add the new test cases alongside the existing
  `test_search_results_page_waits_for_content` /
  `test_non_search_page_does_not_wait_for_search_content` /
  `test_search_results_timeout_proceeds_gracefully` group (around line 3996)
  and the `test_scrape_job*` group (around line 2150).

## Definition of Done

- `is_job` branch added to `_extract_loaded_section`, following the
  established branch pattern (URL-path check + bounded, non-raising
  `wait_for_function`).
- New tests cover: wait triggers on job URLs, wait does not trigger on
  non-job URLs, timeout degrades gracefully, and `scrape_job` returns the
  description text when present.
- Full existing test suite (`tests/test_scraping.py`, `tests/test_tools.py`)
  still passes — no regression to `is_activity`/`is_search`/
  `is_company_people`/`is_details` behavior.
- No changes made to PR #559's territory (the "Show more" click mechanism).
