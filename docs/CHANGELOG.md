# Changelog

Entries start with the release that adopted towncrier. Earlier releases are
described on [GitHub Releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases).

<!-- towncrier release notes start -->

## 4.27.0 (2026-10-10)

### Highlights

- **Multi-line messages.** `send_message` sends line breaks and empty lines as one message. ([#1224](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1224))
- **Apply links.** `get_job_apply_url` returns the apply type and the employer's link. ([#1035](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1035))
- **Sessions recover.** A profile that lost its cookies gets them back before any page. ([#1251](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1251))

### Features

- get_job_apply_url says how a posting takes applications and gives the employer's URL. ([#1035](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1035))
- get_saved_jobs takes a stage to read in-progress, applied and archived tracker jobs. ([#1058](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1058))
- send_message now sends multi-line messages as one message, keeping empty lines. ([#1224](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1224))
- send_message reports the thread a confirmed message landed in, or null on compose. ([#1243](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1243))
- get_job_details adds apply: {type} for postings applied to or closed. ([#1249](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1249))

### Bug Fixes

- A browser install no longer fails when its helper processes end late under load. ([#1220](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1220))
- A profile locked under another host name now says which lock files to remove, and when. ([#1228](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1228))
- A browser install whose size limit was exceeded is now refused even if it exits cleanly. ([#1229](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1229))
- The CLI now says that Ctrl+Z does not pause a browser download. ([#1230](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1230))
- Logout no longer deletes a session another client signed in with while it waited. ([#1231](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1231))
- A lost shared browser now tells clients to reconnect instead of an internal error. ([#1232](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1232))
- send_message now reports an unconfirmed send instead of a bare timeout error. ([#1233](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1233))
- A cancelled job search no longer keeps scrolling its results in the shared page. ([#1234](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1234))
- Tools now refuse a page that ended up off LinkedIn instead of reading it as LinkedIn's. ([#1235](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1235))
- A browser that stopped is shut down, and the next tool call starts a new one. ([#1240](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1240))
- A feed navigation is timed from the request, and one that is never sent still ends. ([#1245](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1245))
- get_job_apply_url reads an Easy Apply that LinkedIn now renders as a button. ([#1250](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1250))
- A profile that opens without its saved cookies loads them before the first page. ([#1251](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1251))
- Exclude new-message compose destinations from conversation references and thread IDs. ([#1255](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1255))
- search_jobs keeps the pages it has when a slow page reaches the end of its time budget. ([#1261](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1261))


## 4.26.2 (2026-10-03)

### Breaking Changes

- Python imports move from `linkedin_mcp_server.scraping` to `linkedin_mcp_server.linkedin`. ([#1189](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1189))
- Every Python name containing "scrap" is renamed, keywords too; error context: `read_*`. ([#1190](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1190))

### Bug Fixes

- `--status` now says another process holds the browser profile instead of failing. ([#1191](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1191))
- On Windows, closing the shared daemon's browser no longer breaks its next browser start. ([#1201](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1201))
- Send messages and return `profile_urn` again on LinkedIn's redesigned profile page. ([#1214](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1214))


## 4.26.1 (2026-09-28)

### Bug Fixes

- send_message reports "Press Enter to Send" and explains how to switch that setting. ([#1109](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1109))
- Keep an open chat overlay from blocking connect_with_person invitations. ([#1110](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1110))
- Avoid needless browser setup when Patchright versions share the cache. ([#1162](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1162))
- Recover browser setup when Windows AppContainer grants block the default temp directory. ([#1163](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1163))


## 4.26.0 (2026-09-27)

### Highlights

- **MCP 2026-07-28.** The server accepts clients on the new protocol and earlier ones. ([#1139](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1139))
- **Sent means sent.** `send_message` confirms delivery from LinkedIn's own receipt. ([#1108](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1108))
- **Restrictions are named.** A restricted account no longer reads as an expired login. ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147))

### Features

- `get_company_posts` takes `max_scrolls` to read further back in a company feed. ([#1104](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1104))
- The opt-in shared browser stays off on network, FUSE and cloud-synced storage. ([#1126](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1126))
- The opt-in shared browser lets running calls finish before a newer owner takes over. ([#1127](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1127))
- Login, logout and browser import ask an idle shared browser to step aside first. ([#1128](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1128))
- The server runs on FastMCP 4 and accepts clients on the 2026-07-28 MCP protocol. ([#1139](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1139))
- The opt-in shared browser talks the 2026-07-28 protocol when its owner offers it. ([#1140](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1140))

### Bug Fixes

- `send_message` reports `sent` for delivered messages instead of `send_unconfirmed`. ([#1108](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1108))
- A shared browser owner that cannot close its browser no longer signals other processes. ([#1122](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1122))
- The Docker image runs Chrome for Testing 153; older images refuse a profile it opened. ([#1123](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1123))
- `CHROME_PATH` and `--chrome-path` keep the server on its own browser. ([#1125](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1125))
- `connect_with_person` returns `connect_unavailable` when the note cannot be filled. ([#1136](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1136))
- `connect_with_person` no longer reports a sent invitation as `send_failed`. ([#1137](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1137))
- A restricted LinkedIn account is reported as restricted, not as an expired session. ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147))


## 4.25.1 (2026-09-26)

### Bug Fixes

- A profile URL whose query mentions another route no longer switches capture mode. ([#1076](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1076))
- `get_job_details` keeps the posting text and reports a missing description. ([#1088](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1088))
- `contact_info` returns a section error, not profile text, when its overlay is missing. ([#1096](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1096))
- Conversation tools no longer return another conversation's thread ID. ([#1102](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1102))
- `send_message` and `get_person_profile` work again after LinkedIn's top card change. ([#1105](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1105))
