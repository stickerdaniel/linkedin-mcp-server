# Fork notes — Voyager messaging

Fork of `stickerdaniel/linkedin-mcp-server`, branched from **4.24.3** at `voyager-conversations`.

## Why this fork exists

`get_inbox` could only see what LinkedIn painted into the sidebar (**16–17 rows**), and recovered
each thread id by **clicking its row, which marks the row read**. So a full inventory was impossible:
you could not ask "has every thread been answered" without altering what you were measuring.

The web client does not work that way. It fetches conversations from `voyagerMessagingGraphQL` and
renders the result, so every thread id is already in the payload as `entityUrn`.

**Measured on Taylor's live mailbox, 2026-09-16:** 400 conversations over 20 cursor pages, still not
exhausted, nothing clicked. The DOM path saw 16–17.

## Relationship to upstream

The two "no Voyager" statements in the package are both scoped to **`send_message`'s recipient
verification** (`message_sender.py`, and the `send_message` docstring in `tools/messaging.py`).
There, the concern is proving you are messaging the right person, and the rule is that recipient
authorization must come from a validated top-card action and a pinned route rather than a private
API. That is a constraint on a **write** path, not a project-wide ban — an earlier version of this
file described it as one, which was wrong.

This change is **read-only and additive**: a new tool, no behaviour change to any existing one, and
`get_inbox` untouched. So it is a reasonable upstream proposal rather than a permanent fork, and it
has been offered as one.

**The divergence is three files:** `scraping/voyager_messaging.py` (new and standalone), plus
additive changes to `scraping/extractor.py` and `tools/messaging.py`. If it is not taken upstream,
rebase on upstream tags rather than re-patching; conflicts should confine to those three.

## What changed

- **`scraping/voyager_messaging.py`** (new) — cursor walk over the conversations API.
- **`scraping/extractor.py`** — new `get_all_conversations(...)` passthrough. `get_inbox` untouched.
- **`tools/messaging.py`** — new `get_all_conversations` tool. `get_inbox` untouched.

## `get_inbox` is left exactly as upstream wrote it

This fork adds **one tool**, `get_all_conversations`, and **does not change `get_inbox`**.

That is deliberate. Upstream declines Voyager on principle, and `get_inbox` is their method; quietly
swapping its mechanism would mean carrying a behavioural fork of a function they maintain, and every
rebase would have to re-litigate it. Keeping the new mechanism in a new tool means the divergence is
purely additive — upstream can change `get_inbox` freely and this fork just takes it.

It also removes a hazard that existed while `get_inbox` had a Voyager path with a DOM fallback: the
DOM path harvests thread ids by **clicking rows, which marks them read**, so any Voyager failure
could turn into a write. With no fallback, that cannot happen.

So the split is:

| question | tool |
|---|---|
| what is at the top of the inbox right now | `get_inbox` (unchanged, DOM) |
| which threads are unanswered / who has gone quiet / reconcile against a record | `get_all_conversations` |

## What LinkedIn's API will and will not filter (measured 2026-09-16)

The query's real variable shape is:

```
(query:(predicateUnions:List((conversationCategoryPredicate:(category:PRIMARY_INBOX)))),
 count:20, mailboxUrn:urn:li:fsd_profile:<me>, nextCursor:<cursor>)
```

| knob | server-side? | evidence |
|---|---|---|
| `category` | **YES** | ARCHIVE, INMAIL, STARRED, SPAM each return a set whose **date range differs** from unfiltered. SPAM reached **2025** rows in ONE call, no paging. |
| `count` | **YES, up to 25** | 5→5, 10→10, 21→21, 25→25. **30+ returns EMPTY, not an error.** |
| `lastUpdatedBefore` | **NO** | `-90d` and `-365d` returned **byte-identical** result sets. Silently ignored. |
| unread / awaiting-reply | **NO such category** | `UNREAD` returns 0 — but so does a deliberate nonsense value, so 0 proves nothing. |

**So: there is no way to jump to a date server-side.** Dormant-contact search must page
backwards through everything newer, at ≤25 per request. `category` is the only filter that
teleports, so prefer it whenever the question fits one.

### ⚠ The trap that runs through all of this

**Three different mistakes all return an empty page rather than an error**: an unknown
`category`, a `count` above 25, and a genuinely empty mailbox. An empty result is therefore
never self-explanatory. That is why `category` is validated against `KNOWN_CATEGORIES` and
`page_size` is clamped — a rejected argument is honest, a silent zero is not. The negative
control that proved it was passing a category that cannot exist and watching it come back
looking exactly like a real, empty answer.

### The efficiency answer: walk once, then sync incrementally

Since the server cannot filter by time, the only real defence is not re-walking. Pass
`stop_at_thread_urns` (the thread urns already in the ledger): the mailbox is recency-ordered,
so once a whole page is threads you already know, everything behind it is older and also known,
and the walk stops. **A daily run then costs one or two pages instead of the whole mailbox**,
and the long walk is paid once.

## Running it

```bash
uv --directory ~/Coding/linkedin-mcp-server run mcp-server-linkedin \
  --chrome-path "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --daemon --tool-timeout 300
```

`--chrome-path` is **required**: the profile was last opened by Chrome 152, and the bundled
patchright browser is 149, which the server refuses as a downgrade.

## Rebasing on upstream

```bash
git fetch origin && git rebase origin/main
```
Conflicts should be confined to the two touched files; `voyager_messaging.py` is standalone.
