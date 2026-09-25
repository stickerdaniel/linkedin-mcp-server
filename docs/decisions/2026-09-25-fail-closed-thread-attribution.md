# Attribute a conversation row only to the thread its own click opened

- Date: 2026-09-25
- Issue: [#1093](https://github.com/stickerdaniel/linkedin-mcp-server/issues/1093)
- Supersedes: none

## Decision

A conversation row earns a thread id only when, after its click, the pathname
carries a thread id (`^/messaging/thread/([^/]+)/?$`) different from the one it
carried immediately before that click. Query, hash and trailing-slash changes
are not moves, and a thread marker in a query is not a thread. The poll budget
stays at twelve requested 100 ms waits.

The first click that does not produce such an id stops the scan. No later row
is clicked, nothing is retried, and the pre-click id is never credited, because
the unresolved click may still land and would then be credited to whichever row
was being polled. A matching row without a click target is skipped; in a
filtered scan it is recorded as the first index gap.

Click scans start from the pages below, chosen because none of them opened a
thread on its own in the dated observation (see Evidence). That is not a
guarantee for every variant; the per-row rule above still decides each row.

| Caller | Text page | Scan page |
|---|---|---|
| `get_inbox` | `/messaging/` | `/messaging/compose/` |
| `get_conversation(linkedin_username=...)`, inbox leg | none | `/messaging/compose/` |
| `get_conversation(linkedin_username=...)`, search fallback | `/messaging/?searchTerm=<name>` | same page |
| `search_conversations` | `/messaging/?searchTerm=<keywords>` | same page |

Bare `/messaging/` is never scanned. A scan that nevertheless starts on a
thread path is not refused: the rule compares each row with its own pre-click
id, so a later click that opens a different thread still counts.

`get_inbox` keeps its text and result URL from bare `/messaging/` and adds one
navigation to the compose page for the clicks. Its text contract is unchanged;
the extra navigation is the accepted cost.

## Resolver

Username resolution converts one scan into the gap-free prefix of matching
rows that precede the earliest barrier: an unresolved click, a matching row
without a click target, or an attributed row that fails the existing exact
display-name check in Python. An index inside that prefix is served; an index at
or beyond it is refused with a `LinkedInScraperException` naming the reason, and
nothing is navigated to or captured.

| Inbox scan | Action |
|---|---|
| Nonempty prefix, with or without a later barrier | Use it; never search to extend it |
| Empty prefix with a barrier | Refuse; do not search |
| No matching row and no barrier, including a row-wait timeout | The existing search fallback, converted by the same rules, with no further fallback |

Rows are never renumbered past a barrier, sorted, or combined across the inbox
and search scans.

## Diagnostics

A stopped listing scan adds `section_errors.<section>` with
`thread_attribution_stopped` for both listings. `get_inbox` alone also reports
`conversation_rows_unavailable` when the compose rows do not attach, because its
text comes from another page and cannot show that the references are missing.
A search whose rows do not attach stays silent, as before. Both messages point
to `get_conversation(thread_id=...)`, which bypasses row attribution. The
messages describe click-derived references only; anchors captured from the text
page keep their normal handling.

## Evidence

Checked 2026-09-25:

- Bare `/messaging/` showed 1 row and was already on `/messaging/thread/<id>/`
  when the row attached. Clicking that row left the pathname unchanged for 3 s.
- `/messaging/compose/` showed 1 row and stayed on `/messaging/compose/` at
  settle and after 7 s. Clicking the row reached `/messaging/thread/<id>/` at
  the first 100 ms sample, with the id bare `/messaging/` had opened.
- `/messaging/?searchTerm=<name>` with 1 matching row stayed on `/messaging/`
  with the query kept and opened no thread.

Unmeasured: with several conversations, compose row count and order compared
with bare `/messaging/`; whether compose opens a thread on other variants;
search with several matches; the timing of a second row's click from compose.

The dated measurement record is private and not in the repository.
[#703](https://github.com/stickerdaniel/linkedin-mcp-server/issues/703) reports
an older variant whose `sections.inbox` carried an inline transcript and whose
thread deep links failed; it says nothing about compose. Neither observation
holds for every variant.

## Boundaries

- Compose and bare-inbox row count and order are not measured with several
  conversations. A shorter or reordered compose list without a stop or
  timeout is not detected, and an index refers to the observed scan.
- A row-wait timeout does not prove an empty list.
- The pathname rule is not proof of member identity, and it does not rule out
  unrelated router activity.
- A stopped scan cannot cancel a click already dispatched: it may still land
  later or mark the row read.
- Twelve requested waits are not a guaranteed 1.2 s deadline.

## Reopening gate

Reading the inbox text from the compose page too, which would save the extra
navigation, needs a measurement with several conversations that compares the
cleaned compose text with the inbox text and the observed row lists of both
pages under matched loading, plus an explicit decision on the public text and
URL contract. Main-text length alone does not answer it.
