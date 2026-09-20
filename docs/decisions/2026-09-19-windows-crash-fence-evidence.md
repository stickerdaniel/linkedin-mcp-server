# Measure the Windows crash fence before implementing it

- Date: 2026-09-19
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: guardian-loss native evidence pending; production implementation stopped

No production guardian or profile fence is selected. The merged #1025 evidence
harness runs in the native Windows daemon matrix on Python 3.12.4, 3.13 and 3.14
against real Job Objects and the real `ProfileLease` backend.

The baseline assigns a long-lived synthetic descendant set to the project owner
Job, gives the owner the last Job handle and the profile lease, then calls
`TerminateProcess` on the owner. The observer retains process handles only. A
real contender first proves and timestamps contention. The observer then closes
its project-Job handle so it cannot delay kill-on-close, and records owner exit,
lease acquisition, descendant exit, and the number of descendants still active
when the lease was acquired. The field
`lease_acquired_with_live_descendant_ns` is the lease-acquisition timestamp when
that count is positive; it does not claim to timestamp owner exit.

The live-guardian candidate starts a guardian before creating the project owner
Job and proves that the guardian is outside that Job. The guardian acquires a
fresh `ProfileLease`; it does not inherit a `LockFileEx` handle or assume that
handle inheritance transfers lock ownership. Before owner termination, it opens
and retains both the project owner Job and the separate browser Job. Native
Python 3.12.4, 3.13 and 3.14 runs of the merged harness established this path:
the contender was rejected while the guardian lived, a browser descendant
survived observed owner death, browser-Job termination preceded drain, the lease
remained unavailable until browser zero, and project-owner termination followed
browser zero. Injected termination, query and timeout failures retained the
lease until the outer harness terminated and drained the failed guardian.

The guardian-loss scenario tests the premise that this candidate can fence the
profile for the whole browser lifetime. It starts the same real contender before
terminating the guardian and requires a failed acquisition before termination.
Stable process handles prove that the guardian, owner and browser descendants
are active, and a transient named-Job query proves nonzero browser
`ActiveProcesses`. The probe then terminates the guardian through the same
`PROCESS_TERMINATE | SYNCHRONIZE` handle used to observe its exit. It jointly
observes guardian exit, contender acquisition, owner and descendant handles,
and browser-Job accounting under one deadline. Named-Job query or process-wait
errors fail the probe and are never interpreted as zero or exit. The observer
never retains a browser-Job handle, so it cannot manufacture safety by delaying
kill-on-close.

The candidate premise will be falsified if native CI observes that, after
guardian exit, the real contender acquires the lease while the owner and at
least one browser descendant remain active and the browser Job still reports
active processes. That unsafe window is the observation under test, not a
claimed result or a desired contract.

## Measurement boundary

The #1025 live-guardian evidence above is from the merged native Windows matrix
on Python 3.12.4, 3.13 and 3.14. This guardian-loss extension was prepared on
Darwin. Darwin can compile, lint, collect and exercise its platform-independent
ordering and survivor contracts, but cannot execute Windows Job Objects,
`TerminateProcess` or the Windows `LockFileEx` backend. Native CI evidence for
the new guardian-loss scenario is pending the pull-request run; no local Windows
outcome or timing is claimed here. Each completed native probe appends its raw
JSON timestamps and process counts to the GitHub Actions step summary.

Readiness uses kernel events and the existing process gate. Exit and acquisition
observation uses process objects and waitable events with fixed deadlines rather
than scheduler sleeps. The whole probe is assigned to a separate outer harness
Job before its gate is released. Every error or timeout causes that harness to
terminate all probe processes and drain to zero. Inner cleanup terminates and
waits through retained process or Job handles before closing them, with no
PID-only fallback. The outer harness is distinct from the project owner and
browser Jobs and retains no handle to either one.

## Production stop boundary

Do not implement the measured candidate topology in production. In particular,
do not make an independently killable guardian the sole owner of the
`ProfileLease` while the owner or browser descendants can remain alive. A green
live-guardian path proves only its orderly and fail-closed behavior while that
guardian survives; it does not repair loss of the sole lease owner.

Production work remains stopped until a different topology demonstrates on all
three native matrix versions that every guardian-loss path keeps the lease
unavailable until the browser Job is proven empty, including termination,
query-error and timeout paths, without relying on an observer-held Job handle.
A native guardian-loss result that does not show the expected unsafe window
would also require investigation before changing this boundary, because it
would contradict the ownership premise this scenario is designed to test.
