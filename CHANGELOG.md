# Changelog

Entries start with the release that adopted towncrier. Earlier releases are
described on [GitHub Releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases).

<!-- towncrier release notes start -->

## 4.26.0 (2026-09-27)

### Highlights

- **MCP 2026-07-28.** The server runs on FastMCP 4 and accepts clients on the new protocol and on earlier ones. ([#1139](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1139))
- **Sent means sent.** `send_message` confirms delivery from LinkedIn's own acknowledgement. ([#1108](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1108))
- **Restrictions are named.** A restricted account is no longer reported as an expired login. ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147))

### Features

- `get_company_posts` takes `max_scrolls` to read further back in a company feed. ([#1104](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1104))
- The opt-in shared browser stays off on network, FUSE or synced storage such as iCloud, OneDrive or Dropbox. ([#1126](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1126))
- The opt-in shared browser lets running calls finish for up to 30 seconds before a newer owner takes over. ([#1127](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1127))
- `--login`, `--logout` and `--import-from-browser` ask an idle shared browser to step aside, and refuse while it is busy. ([#1128](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1128))
- The server runs on FastMCP 4 and MCP Python SDK 2, so clients on the 2026-07-28 MCP protocol can connect. ([#1139](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1139))
- The opt-in shared browser talks to its owner over the 2026-07-28 protocol when offered, and over the earlier handshake otherwise. ([#1140](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1140))

### Bug Fixes

- `send_message` reports `sent` for delivered messages instead of `send_unconfirmed`. ([#1108](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1108))
- A shared browser owner that cannot close its browser no longer signals other processes when it exits. ([#1122](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1122))
- The Docker image runs Chrome for Testing 153 on both architectures, and an older image refuses its profile instead of losing the session. ([#1123](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1123))
- `CHROME_PATH` or `--chrome-path` keeps the server on its own browser instead of joining the shared one. ([#1125](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1125))
- `connect_with_person` returns `connect_unavailable` instead of `custom_note_limit_reached` when the note cannot be filled. ([#1136](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1136))
- `connect_with_person` re-reads the profile before reporting `send_failed`, so a sent invitation no longer comes back as failed. ([#1137](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1137))
- A restricted LinkedIn account is reported as restricted, with steps to recover, instead of as an expired session. ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147))


## 4.25.1 (2026-09-26)

### Bug Fixes

- A profile URL whose query or fragment mentions a search, company people or details route no longer switches capture to that mode. ([#1076](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1076))
- `get_job_details` keeps the posting text and reports `description_missing` when the description heading is absent. ([#1088](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1088))
- `get_person_profile` and `get_my_profile` report a `contact_info` section error when its overlay is missing, instead of profile text. ([#1096](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1096))
- `get_inbox`, `search_conversations` and `get_conversation` no longer return another conversation's thread ID for a row they cannot verify. ([#1102](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1102))
- `send_message` resolves recipients and `get_person_profile` returns `profile_urn` again after LinkedIn changed the profile top card. ([#1105](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1105))
