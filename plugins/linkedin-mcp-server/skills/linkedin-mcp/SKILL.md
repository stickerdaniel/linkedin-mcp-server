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
- `send_message`, `connect_with_person`, `react_to_post`, `comment_on_post` and
  `repost_post` are write actions. Use them only when the user explicitly
  authorizes the exact target and action. Confirm the final message, connection
  note, comment or commentary unless the user has already supplied it.
- The three post actions are publicly attributed to the user's own account, and
  a comment or a repost is content published under their name that a retry
  duplicates. Authorize each post individually; a list of posts the user asked
  you to *find* is not permission to engage with any of them.
- Do not retry a failed write action unless the result proves it was not sent.
  For the post actions that is the `retry_safe` field: while it is false, the
  action may have landed, so open the post instead of calling again. Retrying a
  reaction that did land removes it.

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
  `get_sidebar_profiles`.
- Companies: `get_company_profile`, `get_company_posts`, `search_companies`,
  and `get_company_employees`.
- Jobs: `search_jobs`, `get_saved_jobs`, and `get_job_details`.
- Content: `get_feed` and `search_posts`.
- Messages: `get_inbox`, `get_conversation`, and `search_conversations`.
- Writes: `send_message`, `connect_with_person`, `react_to_post`,
  `comment_on_post` and `repost_post`, subject to the explicit authorization
  rules above. The post actions take one post permalink, of the kind `get_feed`
  and `search_posts` return as a `feed_post` reference.
- Cleanup: use `close_session` only when the user asks to end the managed
  browser session or when the current LinkedIn task is finished and no follow-up
  call is expected.
