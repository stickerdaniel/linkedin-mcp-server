# Record Windows baseline overlap without requiring it

- Date: 2026-09-29
- Related: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Supersedes: none; additive test-assertion classification
- Status: baseline assertion reclassified; no production change

## Decision

The owner-crash baseline records whether a contender observed a live descendant
in its sample after acquiring the profile lease. It does not require that
observation in every trial.

Windows specifies [termination when the last kill-on-close Job handle closes](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_limit_information)
and [release of a terminated process's file locks](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex).
Neither promises that an independently scheduled contender will observe a live
descendant during rundown. Acquisition, the contender's sequential handle checks,
and the observer's completed-rundown timestamp are separate observations.

The baseline reports `descendant_overlap` as `observed` or `not-observed`.
`observed` retains the positive live-descendant witness. `not-observed` means the
post-acquisition sample found none; it does not decide whether they had already
exited at acquisition or exited before their handles were sampled. That label is
neither a native hazard witness nor proof of safe acquisition ordering.

## Assertion classification

| Classification | Boundary |
| --- | --- |
| Permanent guards | Pre-crash lease contention, a live owner and a positive retained-descendant sample before the termination request, eventual acquisition, observed owner exit and complete tracked rundown within the existing bounds. Missing observations and errors remain failures. |
| Intentionally retired | The unconditional positive-overlap requirement of `test_native_owner_crash_releases_lease_before_job_descendants_exit`. Its replacement tests the baseline record and bounded lifecycle, with an honest classification. |
| Residual observation limit | A zero post-acquisition sample cannot order acquisition against the actual kernel exits. No acquisition-versus-rundown observation ordering is imposed on that branch. |

A nonempty list of process handles is not evidence of a live starting cohort.
The baseline therefore samples its already-retained descendant handles before
requesting owner termination. This is a checkpoint, not continuous liveness.
A `not-observed` label cannot rescue a failed starting-cohort or completion guard.
The historical `active_descendants_at_lease_acquire` field remains compatible;
its value is the sample taken after acquisition, not an atomic acquisition-time
snapshot.

## Scope

This classification follows the [default-on contract](2026-09-26-daemon-default-on-contract.md)
and the [knowledge policy](../knowledge-policy.md). Historical positive trials
remain evidence for those trials; aborted attempts are not retrospectively
completed measurements. Evolving run results belong in the evidence ledger, not
this record, and no new native result is asserted here.

The [guardian-loss decision](2026-09-20-windows-guardian-loss-evidence.md) already
superseded the [initial crash-fence proposal](2026-09-19-windows-crash-fence-evidence.md).
This record does not reinstate that rejected topology or supersede the
[conjunction results](2026-09-20-windows-conjunction-fence-evidence.md).
Controlled guardian-loss, candidate fail-closed and conjunction assertions stay
mandatory and unchanged. Production admission, Windows Desktop verification,
the pilot and the daemon's default remain outside this test-only change.
