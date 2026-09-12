---
name: linkedin-mcp
description: Use LinkedIn data for profile, company, job, post, feed, inbox, recruiting, and outreach work through the bundled LinkedIn MCP server. Use only for an explicit LinkedIn request.
---

# LinkedIn MCP

Use the bundled `linkedin` MCP server only when the user explicitly asks for
LinkedIn data or an action on LinkedIn. Do not call LinkedIn tools for unrelated
work, health checks, or background maintenance.

## Operating rules

- Start with the smallest read operation that can answer the request.
- Treat profile, company, job, post, feed, and message results as live LinkedIn
  evidence. Distinguish retrieved facts from inference.
- Keep searches and result pages modest. Do not bulk scrape or spam.
- Never enable the plugin or its MCP server, edit Codex configuration, or start
  a login flow merely because LinkedIn might be useful. If either component is
  disabled, explain that state and stop.
- `send_message` and `connect_with_person` are write actions. Use them only when
  the user explicitly authorizes the exact recipient and action. Confirm the
  final message or connection note unless the user has already supplied it.
- Do not retry a failed write action unless the result proves it was not sent.

## Browser and authentication

The server uses a managed Chromium browser and the user's own LinkedIn session.
Installing or enabling the plugin does not sign in. The first LinkedIn data
request may prepare the browser, import an existing local session, or require a
visible login window. If LinkedIn presents a captcha, two-factor prompt, or
other user-owned authentication step, stop and ask the user to complete it.

If browser setup or authentication is still in progress, report that exact
state and retry once after it finishes. Do not loop, launch extra browser
instances, clear profiles, or replace the user's browser session.

## Tool selection

- Profiles: `get_my_profile`, `get_person_profile`, `search_people`, and
  `get_sidebar_profiles`. `search_people` takes Boolean `keywords` and
  optional `location`, `network`, `current_company`, `past_company`
  (names, slugs or ids; lists accepted), `title`, `industry`, `school`
  (numeric id only), `first_name`, `last_name` and `profile_language` facets
  plus `max_pages`; every page is one navigation, and each company name that
  is not already cached costs one or two more. LinkedIn ignores `title` on
  its current results page: put the title in `keywords` as a quoted phrase.
  `title` as the only criterion is refused rather than returning an
  unfiltered list. Only `current_company` and `first_name` are verified
  live; `past_company`, `industry`, `school`, `last_name` and
  `profile_language` are unverified against live LinkedIn, and a wrong
  parameter name is ignored, so cross-check results.
  The result carries `people` rows (`name`, `degree`, `headline`, `location`,
  `snippet`, `url`) parsed from the page text and a `result_count`; `url` is
  null when a card could not be paired with a profile link, so keep the raw
  text for anything a row does not carry.
- Companies: `get_company_profile`, `get_company_posts`, `search_companies`,
  and `get_company_employees`. `search_companies` takes optional `industry`,
  `size`, `hq_location` and `has_jobs` facets plus `max_pages`; narrow there
  first so `enrich_companies` only spends navigations on the shortlist.
  `industry` and `size` are unverified against live LinkedIn, and a wrong
  parameter name is ignored, so cross-check results. The
  result carries `companies` rows (`name`, `industry`, `location`, `tagline`,
  `followers`, `url`) parsed from the page text and a `result_count`.
- Company research at scale: `enrich_companies` (paced, cache-first; pass
  `about=true` for About-tab facets at one extra navigation per company),
  `enrich_company_deep` for one company's facets plus open roles,
  `get_company_cache` to read a record, and `query_company_cache` to filter
  the cache by industry, headquarters, headcount, hiring, or founding year
  without touching LinkedIn.
- Jobs: `search_jobs`, `get_saved_jobs`, and `get_job_details`.
- Content: `get_feed` and `search_posts`.
- Messages: `get_inbox`, `get_conversation`, and `search_conversations`.
- Writes: `send_message` and `connect_with_person`, subject to the explicit
  authorization rules above.
- Cleanup: use `close_session` only when the user asks to end the managed
  browser session or when the current LinkedIn task is finished and no follow-up
  call is expected.
