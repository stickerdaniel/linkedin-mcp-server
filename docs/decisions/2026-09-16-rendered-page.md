# Read the rendered page

- Date: 2026-09-16
- Supersedes: none

This project reads the page LinkedIn has rendered, in the signed-in browser, with
`innerText` and URL navigation.

`messengerConversations`, `voyagerMessagingGraphQL`, and `/voyager/api/` are
LinkedIn private APIs. Calling them is reverse engineering. This repo stays
online because we do not do that.

If LinkedIn has not rendered the data on a page, the tool cannot read it.
`get_inbox` only sees the conversations in the messaging sidebar.

The `send_message` comments are this same rule.
