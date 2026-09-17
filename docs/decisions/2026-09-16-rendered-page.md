# Read the rendered page

- Date: 2026-09-16
- Supersedes: none

This project reads the signed-in page: `innerText`, URL navigation, and
JSON or document bodies that page load already fetched. Issuing LinkedIn
private API requests is reverse engineering. This repo stays online because
we do not do that.

`messengerConversations`, `voyagerMessagingGraphQL`, and `/voyager/api/`
are those private APIs.

`get_inbox` only sees the conversations in the messaging sidebar.

The `send_message` comments are this same rule.
