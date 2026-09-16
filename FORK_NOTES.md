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

## Why it can never go upstream

The upstream author refuses Voyager **on principle** — the only two mentions of it in the package are
instructions not to use it (`message_sender.py`, `tools/messaging.py`). This is a permanent fork of
those two files plus one new module. Rebase on upstream tags rather than re-patching.

## What changed

- **`scraping/voyager_messaging.py`** (new) — cursor walk over the conversations API.
- **`scraping/extractor.py`** — `get_inbox(limit, backend)` + `get_all_conversations(...)`.
- **`tools/messaging.py`** — `backend` arg on `get_inbox`, new `get_all_conversations` tool.

## The flag

`get_inbox(backend=...)` takes `default` | `auto` | `voyager` | `dom`.
`default` defers to **`LINKEDIN_MESSAGING_BACKEND`**, itself defaulting to `auto`.

| value | behaviour |
|---|---|
| `dom` | original scrape. Sees ~16–17 rows, **click-marks them read**. |
| `voyager` | conversations API. Whole mailbox, paged, clicks nothing. |
| `auto` | Voyager, falling back to `dom` on any failure. |

## ⚠ Why the deployed default is `dom`, not `auto`

**The routines parse `get_inbox`'s TEXT block**, and the Voyager renderer does not yet emit
**per-message timestamps or preview text** — both of which inbox triage reads to decide what is new
and what a message says. Flipping to `auto` before the renderer carries those would silently degrade
triage: it would return *more* threads carrying *less* per-thread detail, which is the worse trade
for the routine that runs every day.

So: `dom` stays the default for `get_inbox`. Use **`get_all_conversations`** for census work, where
structure is what matters and the text block is irrelevant.

**Next change that unblocks `auto`:** render `lastActivityAt` as a local timestamp and pull the last
message body out of the `Message` entities already present in `included`, so the text block is a
superset of the DOM one rather than a differently-shaped peer.

## Two traps worth keeping

1. **The page-load query silently ignores cursor variables.** Appending `count` or
   `lastUpdatedBefore` to it returns **HTTP 200 and the identical first page**. A 200 that drops your
   parameter is indistinguishable from one that honoured it. The cursor-bearing query is a *different
   persisted queryId*, issued by the Load-more button, and LinkedIn rotates the hash every deploy —
   so it is **discovered at runtime, never pinned**.
2. **Do not regex a cursor out of `json.dumps` output.** Python emits `"nextCursor": "..."` with a
   space LinkedIn's wire format lacks, so the match fails and the walk ends after one page, looking
   exactly like a mailbox that fit on one page. Walk the metadata structurally.

Also: the sidebar virtualizes and recycles nodes — its row count was observed going **17 → 10 while
more conversations were loading**. Never terminate a loop on it.

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
