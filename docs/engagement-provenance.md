# Post engagement integration

Reviewed 2026-10-06; integrated on upstream main `9e5375b` after the initial
review against `c8e9be0705591d9982f98e36d5a797e44ee0ffc2`.

The post-reference parser, engagement owner, tool wiring, and initial unit/DOM
tests adapt Dan Stephenson's [PR #1027](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1027),
revision `227e0ad7a580a046a2e9ef783f804cc8ab9f63dc`. That revision uses this
repository's Apache-2.0 license. The repository LICENSE and NOTICE apply and
are retained. This integration does not claim the adapted implementation as
original work.

[PR #1200](https://github.com/stickerdaniel/linkedin-mcp-server/pull/1200), revision
`55fb2344a680890645c037fddff871d1af6a1791`, was reviewed as an alternative.
Its global editor targeting and draft-inclusive text acknowledgment were not
used. This branch is a follow-up integration of existing proposals, with
explicit personal/company actors, verified author mentions, and exact-parent
comment replies. It overlaps with #1027 and #1200 and identifies that overlap
for maintainer review rather than claiming an independent implementation.

The integration updates the renamed `linkedin` package and current rendered
origin boundary. It exposes only likes and comments, with confirmation flags
defaulting to false. It removes ordinal reaction-menu selection and repost
execution, requires explicit actor selection, and preserves uncertain outcomes
after a possible write as `retry_safe=false`.

## Observed UI support

The English SDUI post-detail controls, actor picker, personal first-H2 topcard,
company admin-page author links, and mention suggestions were inspected on
2026-10-06. The actor picker and mention suggestions expose names and avatars,
but no entity URLs. The implementation binds a requested actor URL to its
rendered name and stable image asset, then requires both in a unique picker
option. A company admin redirect is accepted only when rendered links preserve
the requested company identity. Missing or ambiguous mappings, including a
shared avatar asset across different picker identities, refuse.

Actor selection and mention labels currently support English only. Legacy
reaction controls can expose `aria-pressed`; the observed SDUI control exposes
reaction state through its English label. Unknown controls or states refuse.
The exact post route and pinned action row own publication. A bounded adjacent
comment column owns acknowledgment on SDUI layouts where comments are siblings
of the action row. An existing reaction is preserved.

`mention_author=true` prefixes a real rich-text mention of the rendered post
author, followed by a space and the supplied comment. Selection requires the
author's linked identity, name, and avatar; publication requires the same
noneditable mention token to remain in the editor. Plain `@name` text is not a
verified mention. If mention selection fails, no comment is submitted; an
unpublished draft may remain for inspection.

Synthetic Chromium tests exercise these algorithms, ownership changes,
off-site refusal, and ambiguous layouts without contacting LinkedIn. They do
not establish support for every account variant. A successful comment result
requires a new, unique rendered comment URN with the exact body and selected
actor's linked identity and avatar after submission. Existing URNs, arbitrary
matching text, and another author's text cannot acknowledge a publication.
Changed drafts are preserved. Reaction acknowledgment uses only the pinned
actor and toggle; lost ownership leaves an uncertain result, which requires
inspection before retrying. These tools operate within the
active browser session; tenant scheduling and account isolation remain the
responsibility of the caller.


Comment discovery and replies use new exact rendered comment-URN ownership logic.
Existing proposals reviewed, not copied: #571 (`d585dabad09007315088ded5ac1fb3c0910b8795`)
and #683 (`685471e05d04ae57a475be3204e33c702bb44d16`) return whole-post text/profile
references and pagination on the former package layout; #339 takes an official-API
rewrite with OAuth. This integration adds bounded rendered body/reference
reads and actor-verified replies without those alternate backends. It also
partially addresses issue #828's comment-identity gap: only observed comment
URNs and proven rendered ancestry are returned. It does not infer canonical
post URNs, load every hidden reply, or implement comment pagination.

A reply may follow unchanged rows that were present before its exact Reply
click. The guard snapshots those rows' nodes, order, comment identity, body,
and author, then checks them again before submission. It does not infer their
parentage from adjacency. Added, replaced, reordered, or edited rows refuse
publication. This supports an observed parent with an existing reply while
retaining the causal new-editor and exact-parent checks.

Further account validation on 2026-10-07 exposed two pre-dispatch variants:
an actor Save can replace only the reaction bar, and a verified rich mention
can leave a single layout newline in its editor. Reaction controls can be
rebound only before dispatch, within the same connected, rediscovered post
and verified actor, with its original bare root identities preserved. A
changed route, actor, root identity, or ambiguous bar refuses. After dispatch,
the clicked toggle remains the only reaction receipt. Mention typing accepts
the single newline only after rechecking its exact draft, token and actor;
concurrent content is preserved and refused.
