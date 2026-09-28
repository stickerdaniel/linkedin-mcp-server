# Changelog

Entries start with the release that adopted towncrier. Earlier releases are
described on [GitHub Releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases).

<!-- towncrier release notes start -->

## 4.26.0 (2026-09-27)

### Features

- get_company_posts now accepts max_scrolls to read further back in a company feed. ([#1104](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1104))
- The opt-in shared browser now stays off when its profile or state directory is on network, FUSE or synced storage such as iCloud, OneDrive or Dropbox, and that server drives its own browser with one warning. ([#1126](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1126))
- The opt-in shared browser now refuses any tool call that is not tracked for cancellation, finishes calls already running for up to 30 seconds before handing over to a newer owner, and lets a client with a different configuration drive its own browser instead of waiting. ([#1127](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1127))
- With the opt-in shared browser, a confirmed --logout, --login or --import-from-browser now asks an idle shared browser to step aside before changing the profile, and refuses while it is busy. ([#1128](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1128))
- The server now runs on FastMCP 4 and MCP Python SDK 2, so clients that speak the 2026-07-28 MCP protocol can connect as well as clients on earlier protocol versions. ([#1139](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1139))
- With the opt-in shared browser, a client now reaches the shared browser owner over the 2026-07-28 MCP protocol when the owner offers it, and still over the earlier handshake when it does not, with calls that may already have acted still reported as an unknown outcome rather than repeated. ([#1140](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1140))

### Bug Fixes

- send_message now reports sent when LinkedIn shows the message under its server ID, in an open thread and in the first message of a new thread, instead of returning send_unconfirmed for every delivered message. ([#1108](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1108))
- An opt-in shared browser owner that cannot close its browser now exits without signalling any process itself, leaving cleanup to the crash guardian and Windows Jobs as when a direct server's host quits, and a Windows owner no longer ends a process whose Job membership it could not determine. ([#1122](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1122))
- The Docker image now runs Chrome for Testing 153 on both architectures, and a profile it opens can no longer be reopened by an older image, which refuses it with a message instead of losing the session. ([#1123](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1123))
- A server started with CHROME_PATH or --chrome-path no longer joins the opt-in shared browser and keeps driving its own browser. ([#1125](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1125))
- `connect_with_person` no longer reports `custom_note_limit_reached` when filling the note fails while LinkedIn still shows the note field; it sends nothing and returns `connect_unavailable`. ([#1136](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1136))
- `connect_with_person` now re-reads the profile once more before reporting `send_failed`, so an invitation LinkedIn already recorded no longer comes back as failed. ([#1137](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1137))
- A LinkedIn account restriction is now reported as such, with how to get back in, instead of as an expired session; the server no longer waits for a login that cannot complete or reopens login windows. ([#1147](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1147))


## 4.25.1 (2026-09-26)

### Bug Fixes

- Profile URLs whose query string or fragment mentions a search, company people or details route no longer switch capture to that mode. ([#1076](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1076))
- get_job_details now keeps captured posting text and reports a description_missing section error when the expected description heading is absent. ([#1088](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1088))
- The contact_info section in get_person_profile and get_my_profile now reports a section error when its overlay cannot be found instead of returning underlying profile text and links. ([#1096](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1096))
- get_inbox, search_conversations and get_conversation by username now attribute a thread id to a conversation only when clicking it visibly opens that thread, and report or refuse a row they cannot verify instead of returning another conversation's id. ([#1102](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1102))
- send_message no longer refuses every recipient with recipient_resolution_failed, and get_person_profile returns profile_urn again, after LinkedIn nested the profile top card and changed its name heading. ([#1105](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1105))
