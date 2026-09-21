# Reject static Windows lock-region inversion

- Date: 2026-09-21
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: compatibility candidate falsified; native execution pending; production implementation stopped

The conjunction evidence assigned profile-lock byte 0 to the owner and byte 1 to
an external guardian. Inverting those regions does not create a mixed-version or
cold-boundary solution. A current or legacy entrant that knows only the
production `LockFileEx` protocol still tests byte 0. Either static assignment
leaves one isolated holder-loss path in which that entrant can acquire while the
other holder and protected browser lifetime survive.

This record is additive to [the conjunction evidence](2026-09-20-windows-conjunction-fence-evidence.md).
It does not change that probe's measured claims.

## Evidence added

The native probe now uses a separate actor that opens `profile.lock` itself and
models only the current source byte-zero `LockFileEx` primitive under a private,
stable root: acquire one byte at offset 0, validate the locked file identity and
current path, and retain that region behind an event barrier. It does not call
the conjunction admission helper. It does not model the full `ProfileLease`
canonicalization, path hardening, retry, reference counting, handoff or lifecycle
behavior. This is independent structural/model evidence for the primitive, not a
replay of a released predecessor artifact.

Five scenarios expose the unsafe outcome explicitly:

1. With owner=0 and guardian=1, forced owner death releases byte 0 while the
   guardian remains alive with a retained browser Job and live descendants. The
   byte-0 actor can acquire before guardian drain completes.
2. With owner=1 and guardian=0, forced guardian death releases byte 0 while the
   owner is paused before cleanup and the browser Job and tracked descendants
   remain live. The byte-0 actor can acquire before owner drain.
3. With the inverted assignment, a transient byte-0 admission can release before
   the guardian arms. A second byte-0 actor can acquire in that window, so the
   guardian's byte-0 acquisition is contended and neither `ARMED` nor browser
   launch authority is published.
4. The post-DISARM case is structural/model evidence. A guardian's clean exit
   models byte-0 release, and a supervisor-owned manual event marks an outer
   mutation as active while the owner retains byte 1. No production DISARM,
   reference-count or mutation operation runs. The byte-0 actor can acquire
   before the modeled mutation completes.
5. The no-browser case and its fields and conclusion are structural/model
   evidence. The owner retains byte 1, no guardian starts, and a manual hold
   models an exclusive mutation. No production reference-count, mutation or
   lifecycle operation runs. The byte-0 actor can acquire concurrently.

Named events establish admission, ARM and model barriers. Stable process handles
establish holder survival and exit. The post-DISARM event is a supervisor-owned
model marker, not a production protocol event. Browser Job accounting and
tracked child process handles establish the surviving browser
lifetime in the holder-loss cases. Lock acquisition is the result under test;
timestamps are not proof.

A green falsification scenario means the unsafe acquisition was observed. It is
not a production safety claim. Darwin exercises the platform-independent
byte-zero actor contract and explicitly skips all native scenarios. Native
Windows execution remains pending for this record.

## Decision and remaining boundary

Byte inversion is rejected as a mixed-version or cold-boundary solution. Neither
static two-region assignment protects against a byte-0-only entrant across both
isolated holder-loss cases: owner=0/guardian=1 fails after owner loss, while
owner=1/guardian=0 fails after guardian loss. The inverted layout also exposes
pre-ARM, post-DISARM with an outer mutation still active, and no-browser
exclusive-mutation windows.

A real released-predecessor package or artifact has not been executed. Therefore
released-artifact compatibility, installer behavior, historical path handling
and an enforceable cold upgrade boundary remain unproved. The probe also does
not prove production Job handoff, launch integration, simultaneous holder loss,
guardian respawn, or general rename/recreate safety outside its private harness
root.

Production implementation remains stopped. This evidence changes no production
module, runtime default, feature flag, protocol version or workflow.
