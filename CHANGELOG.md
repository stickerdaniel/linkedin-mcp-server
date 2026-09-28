# Changelog

Entries start with the release that adopted towncrier. Earlier releases are
described on [GitHub Releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases).

<!-- towncrier release notes start -->

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
- The Docker image runs Chrome for Testing 153, and older images refuse its profiles. ([#1123](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1123))
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
