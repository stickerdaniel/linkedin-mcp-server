# Measure a conjunctive Windows profile fence

- Date: 2026-09-20
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: native conjunction evidence passed; production implementation stopped

The first Windows crash-fence probe showed that an external guardian can retain
one lease and drain browser processes after owner loss. The follow-up proved
that making that guardian the sole lease holder is unsafe: guardian loss
reopens admission while the owner and browser descendants can remain alive.
Those measurements are recorded in [the original crash-fence record](2026-09-19-windows-crash-fence-evidence.md)
and [the guardian-loss record](2026-09-20-windows-guardian-loss-evidence.md).
This record is additive and does not supersede either result.

The remaining user-mode candidate divides the real `profile.lock` into two
one-byte regions. The owner holds A at offset 0 and an external guardian holds B
at offset 1. Admission is conjunctive and ordered: acquire A, acquire B on the
same descriptor, release B strictly, and retain A. B contention rolls A back.
A B-unlock error closes the descriptor and fails admission, because success
cannot be reported while the transient region may still be held.

## Facts encoded by the probe

The probe uses local `LockFileEx` and `UnlockFileEx` bindings with
`msvcrt.get_osfhandle`, an `OVERLAPPED` carrying the selected offset, and a
length of one byte. Only `ERROR_LOCK_VIOLATION` is contention. Every other
Win32 error propagates.

Every actor opens `profile.lock` itself under the private parent root controlled
by the harness. It first acquires its intended A or B region. Only after that
lock succeeds does it compare the descriptor's volume serial and file index
with the identity recorded by the parent, then verify that the descriptor still
names the current path entry. An identity or path mismatch strictly unlocks and
closes the descriptor and cannot publish success. No file or CRT handle is
inherited; only stable process handles cross into actors.

The native scenarios use named events, process handles and process-object waits
for readiness and exit ordering. Timestamps, where retained by older scenarios,
are diagnostic only. The whole probe is assigned to the existing outer harness
Job before its start gate opens. Inner named-Job query handles are opened only
for one query and immediately closed. Cleanup terminates through retained
process or Job handles, waits for exit or Job zero, then closes handles. It has
no `taskkill` or PID-only cleanup fallback.

The matrix covers these claims:

1. `conjunction-lock-regions` isolates A and B, proves that B release leaves A
   held, and admits a later process only after the retained A descriptor closes.
2. `conjunction-publication` permits `ARMED` only after the guardian acquires B
   and rechecks a stable owner handle. Browser start remains behind an event
   gate until then. A guardian paused until after owner death never publishes
   `ARMED`.
3. `conjunction-owner-loss` keeps B held after browser Job zero until the
   harness observes `ZERO_PROVEN` and grants `ALLOW_B_RELEASE`.
4. `conjunction-guardian-loss-clean-close` makes the surviving owner stop at
   observed guardian exit, drain the browser Job and tracked descendants, and
   release A only after zero. It makes no respawn claim.
5. The `terminate-error`, `query-error` and `drain-timeout` owner-loss branches
   retain B and guardian identity while an external process can acquire A but
   not B. Only the outer harness may end and drain those failed proofs.

## Native result and remaining hypotheses

GitHub Actions run
[`35534298407`](https://github.com/stickerdaniel/linkedin-mcp-server/actions/runs/35534298407)
executed every conjunction scenario successfully on Windows with Python 3.12.4,
3.13 and 3.14 at commit `aefdd0c2773eeacb36fe8740966d21d640c17e8c`.
The run confirms the five probe claims above for the private harness topology,
including both isolated holder-loss paths and all three fail-closed guardian
faults. Darwin separately exercised the platform-independent ordering, cleanup
and error-preservation contracts.

These green native runs do not establish a production admission algorithm,
TOCTOU freedom, production Job handoff, guardian respawn, frontend or launch-gate
integration, simultaneous owner and guardian death, Direct/Login/Import paths,
old-client behavior, or general rename/recreate safety outside the measured
private harness root. The post-lock identity checks do not establish production
TOCTOU freedom. Simultaneous owner and guardian death remains explicitly unsafe
and unmeasured. No production module or workflow changes in this evidence step.

## Production boundary

Production implementation remains stopped. A later decision must evaluate the
native results and separately close the unmeasured integration and compatibility
boundaries before this topology can guard a real profile.
