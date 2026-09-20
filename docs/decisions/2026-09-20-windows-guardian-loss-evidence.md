# Reject a sole Windows guardian lease holder

- Date: 2026-09-20
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Supersedes: [Measure the Windows crash fence before implementing it](2026-09-19-windows-crash-fence-evidence.md)
- Status: candidate falsified; production implementation stopped

The earlier evidence established that an external guardian can retain the real
`ProfileLease` and both relevant Job handles after forced owner death. While the
guardian remains alive, it can terminate and drain the browser Job, release the
lease only after browser zero, then terminate and drain the project owner Job.
Injected termination, query and drain-timeout failures retain the lease and Job
handles until the outer harness terminates the failed guardian.

That result did not cover loss of the guardian itself. The guardian was the sole
lease holder, so its death could release profile admission while the owner and
browser descendants remained alive. This record adds that missing native
measurement and rejects the measured topology.

## Guardian-loss measurement

The harness starts the same candidate guardian outside the project owner Job,
then starts an owner and long-lived synthetic browser descendants. A real
contender is armed before termination and must first prove that the lease is
busy. Stable process handles prove that the guardian, owner and tracked browser
descendants are alive, while a transient named-Job query proves nonzero browser
`ActiveProcesses`.

The harness terminates the guardian through the same
`PROCESS_TERMINATE | SYNCHRONIZE` handle used to observe its exit. Guardian exit
and contender acquisition are sticky observations under one deadline. Only
after both have occurred does the harness sample owner and descendant handles
and browser-Job accounting. It checks the owner again after the Job query so an
exit during that query cannot produce a stale positive result. The contender's
actual acquisition timestamp must follow the termination request and cannot be
later than the acquisition observation.

Exited descendants are removed from subsequent wait sets. Owner exit or loss of
all descendants fails immediately, and the monotonic deadline is checked before
every wait. Query and wait errors are failures, never evidence of an empty Job.
The observer never retains a browser-Job handle, so it cannot manufacture safety
by delaying kill-on-close. Every error or timeout falls back to the outer
harness Job, which terminates all probe processes and drains to zero.

## Native result

GitHub Actions run
[`35519953194`](https://github.com/stickerdaniel/linkedin-mcp-server/actions/runs/35519953194)
executed the guardian-loss assertion successfully on Windows with Python 3.12.4,
3.13 and 3.14 at commit `7caa2c3755c25425d216622601bda50bce54cc5f`.
Each run observed all of the following:

1. The real contender was rejected before guardian termination.
2. The guardian exited after the stable-handle termination request.
3. The contender's acquisition was observed after guardian exit, and its
   acquisition timestamp followed the termination request.
4. The owner and at least one tracked browser descendant were still alive at
   that acquisition observation.
5. The browser Job still reported active processes.

The sole-guardian lease topology is therefore falsified. Guardian death can
reopen profile admission before the owner and browser descendants drain.
Additional processes reported by browser-Job accounting are permitted; the
safety property requires nonzero accounting and independently tracked live
descendants, not equality between those counts.

## Production boundary

Do not implement an independently killable guardian as the sole owner of the
`ProfileLease` while the owner or browser descendants can remain alive. A green
live-guardian path proves only behavior while that guardian survives.

Production work remains stopped until a different topology demonstrates on all
three native Windows matrix versions that every guardian-loss path keeps the
lease unavailable until the browser Job is proven empty. The proof must include
termination, query-error and timeout paths without relying on an observer-held
Job handle. Mixed-version entry remains a separate gate: guardian-incompatible
owners must be excluded through a proven cold compatibility boundary before any
new fence can protect production admission.
