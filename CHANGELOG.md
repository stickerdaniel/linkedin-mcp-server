# Changelog

Entries start with the release that adopted towncrier. Earlier releases are
described on [GitHub Releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases).

<!-- towncrier release notes start -->

## 4.25.1 (2026-09-26)

### Bug Fixes

- Profile URLs whose query string or fragment mentions a search, company people or details route no longer switch capture to that mode. ([#1076](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1076))
- get_job_details now keeps captured posting text and reports a description_missing section error when the expected description heading is absent. ([#1088](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1088))
- The contact_info section in get_person_profile and get_my_profile now reports a section error when its overlay cannot be found instead of returning underlying profile text and links. ([#1096](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1096))
- get_inbox, search_conversations and get_conversation by username now attribute a thread id to a conversation only when clicking it visibly opens that thread, and report or refuse a row they cannot verify instead of returning another conversation's id. ([#1102](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1102))
- send_message no longer refuses every recipient with recipient_resolution_failed, and get_person_profile returns profile_urn again, after LinkedIn nested the profile top card and changed its name heading. ([#1105](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1105))
