# Throttling is a status

- Date: 2026-09-23
- Supersedes: none

LinkedIn says it is throttling in the one place that is not prose: the HTTP
status. Read that, and never the words on the page.

Nothing here read it. The response listeners that exist read payload bodies
for post permalinks, and `detect_rate_limit` matches four English phrases in
the body of short pages without a `<main>`. A page whose own document was
served while the requests that fill it were refused passes that check, renders
little or nothing, and fails later on something no branch of
`raise_tool_error` classifies. `mask_error_details` then reduces it to "Error
calling tool", so the client is told nothing and the failure reads as a
parser bug.

So the statuses are recorded by a listener on the page (`core/throttle.py`),
installed at browser start and cleared as each tool call starts. Only LinkedIn's
own hosts count, since a proxy's 429 is not LinkedIn asking for a wait, and a
response counts only for the call that sent its request. When the call
fails anyway, `raise_tool_error` appends one sentence: the statuses, how many
requests, the latest path without its query, and `Retry-After` when LinkedIn
sent a number. An unclassified failure becomes a `ToolError` carrying that
sentence, chained to the catch-all's redacted copy, so it survives masking and
the daemon middleware still finds the failure's type one hop down.

The record is evidence, never a decision. It raises nothing, retries nothing,
and leaves a call that succeeds untouched, because a refused beacon or prefetch
can cost a page nothing. Classifying a refused navigation as a rate limit is a
separate concern for the navigation code.

Observing is not issuing. The listener reads the status of requests the page
made for itself; it sends nothing and reads no body. The private-API rule
([2026-09-16](2026-09-16-rendered-page.md)) is about what this server asks
LinkedIn for, and this asks for nothing.

Status 999 is treated as 429. It is LinkedIn's "Request denied", the same
answer under a name only it uses.
