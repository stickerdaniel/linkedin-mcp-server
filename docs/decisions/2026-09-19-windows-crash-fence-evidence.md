# Measure the Windows crash fence before implementing it

- Date: 2026-09-19
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: Windows evidence pending

No production guardian or profile fence is selected yet. The W1 evidence step adds
a native Windows probe to the existing Windows daemon CI matrix. It measures two
process arrangements against real Job Objects and the real `ProfileLease` backend.

The baseline assigns a long-lived synthetic descendant set to the project owner
Job, gives the owner the last Job handle and the profile lease, then calls
`TerminateProcess` on the owner. The observer retains process handles only. It
closes its project-Job handle before termination so it cannot delay
kill-on-close, then records owner exit, lease acquisition, descendant exit and
the number of descendants still active when the lease was acquired.

The candidate starts a guardian before creating the project owner Job and proves
that the guardian is not a member of that specific Job. The guardian acquires a
fresh `ProfileLease` itself; it does not inherit a `LockFileEx` handle or assume
that handle inheritance transfers lock ownership. It also retains the separate
browser Job handle. After observing owner death it explicitly terminates that
Job, records repeated `ActiveProcesses` queries and releases the lease only
after a successful zero result. A query error or the fixed drain deadline is a
failed probe, never an empty Job.

## Measurement boundary

This commit was prepared on Darwin. Darwin can compile, lint and collect the
probe, and can exercise the platform-independent drain ordering, query-error and
timeout branches. It cannot execute Windows Job Objects, `TerminateProcess` or
the Windows `LockFileEx` backend. There are therefore no native timings or
claimed Windows outcomes in this record. The CI runs on Python 3.12.4, 3.13 and
3.14 are the first measurements, and each completed probe appends its raw JSON
timestamps and process counts to the GitHub Actions step summary. The probe uses
kernel events, the existing process gate, 30-second deadlines and process-object
state instead of sleeps as readiness or exit evidence. It exercises forced termination only; an
unhandled-crash variant is deferred because this step has no safe helper that
improves the teardown semantics beyond native `TerminateProcess` without also
introducing a separate crash mechanism to validate.

## Next production decision

A production change remains blocked until all native matrix runs report both
observations: the baseline acquires the lease while at least one project-Job
descendant is active, and the external guardian keeps the lease unavailable
until its browser Job reports `ActiveProcesses == 0`. If either observation
fails, the corresponding premise is falsified and the guardian design must not
be implemented from this evidence.
