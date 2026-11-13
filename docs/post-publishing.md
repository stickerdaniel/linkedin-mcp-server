# Personal-post publishing (experimental)

`prepare_post` and `publish_post` adapt Said Hammane's implementation in
[PR #1334](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1334),
reviewed at commit
[`86afc38`](https://github.com/stickerdaniel/linkedin-mcp-server/commit/86afc38ce7ba6cf636c1ae78d02d9bcb48dcb514),
to the current workflow-owner modules, tool errors, and facade contracts.
The adaptation remains under this repository's Apache-2.0 license.

The two-step flow asks for approval of the exact text, personal author, and
Anyone audience. A preview expires after ten minutes, belongs to its browser
page, and permits one attempt. A changed composer, actor, audience, or draft
is refused. An uncertain result is not permission to retry: inspect LinkedIn
before taking another action.

## Verification boundary

On 2026-11-02, a read-only inspection of one signed-in English account's
composer showed its author as text without the profile link required by the
identity guard. That variant is unsupported and is refused before typing.
The inspection used base revision `17be1db` plus this implementation's composer
checks; it did not attempt publication. No live publication has been validated.

Offline unit tests and Chromium tests on synthetic pages cover the approval,
ownership, race, and receipt checks. They verify the algorithm, not LinkedIn's
current markup. English and French label tables do not establish live coverage
for either language. Images, videos, polls, company posts, and other audiences
are unsupported. Do not publish a live post as part of routine testing.
