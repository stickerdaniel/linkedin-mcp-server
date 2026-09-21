# Measure Windows Job topology before integration

- Date: 2026-09-21
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: native execution pending; production implementation stopped

This stage adds synthetic Windows Job-topology evidence only. It does not use
Patchright or Chromium, does not demonstrate browser containment, and does not
measure Claude Desktop or any other released host. The daemon remains opt-in
and production integration remains stopped.

## Experiments

The native probe runs inside an outer harness Job and uses named events and
stable process handles to order and observe four topologies:

1. A process already in the outer Job attempts to assign itself to a new inner
   Job, then creates a child whose membership in both known Jobs is observed.
2. An owner in an inner Job carrying `JOB_OBJECT_LIMIT_BREAKAWAY_OK` creates a
   gated guardian candidate with `CREATE_BREAKAWAY_FROM_JOB`. A separately
   created ordinary child witnesses that the owner Job does not automatically
   release every child.
3. The same creation is attempted without `BREAKAWAY_OK`. Refusal and retained
   membership are distinct reported outcomes and cannot be classified as a
   successful breakaway.
4. Owner, guardian and dummy browser-descendant processes are placed under one
   disposable common ancestor Job. With release events still withheld, every
   retained process object must be active immediately before termination, then
   signal with the exact common-ancestor exit code 204. Post-exit membership
   queries are diagnostic because Windows may no longer answer them.

The outer harness remains the final cleanup authority, including failures while
preparing or starting the probe. Subprocesses are registered immediately after
creation, cleanup is bounded, and cleanup errors do not replace the primary
failure. Timestamps are diagnostics; named events, pre-termination membership
and stable process-object exit codes establish ordering and causality.

## Initial result

Native execution pending. This record intentionally predicts no self-assignment,
breakaway, ambient-host or exit-order result before the Windows CI matrix runs.
The green platform-independent tests cover result classification, membership
matrix interpretation, error preservation and scenario routing, not native Job
semantics.

The common-ancestor experiment is a structural counterexample to treating
owner and guardian Jobs as independent. A host or launcher that can terminate a
Job containing both holders can remove both, regardless of separate inner
roles. This is not evidence about simultaneous-death admission safety and is
not production browser evidence.

## Remaining runtime evidence

Before production work can resume, evidence is still required for:

- the locked Patchright Node, Chromium and crashpad process-creation sequence;
- the actual released installation forms;
- Claude Desktop's ambient Job membership and limits;
- handle transfer and acknowledgement across the proposed holders;
- authority at the COMMIT boundary;
- the filesystem and lifecycle work assigned to PR0c.

Production remains stopped until those boundaries and the native topology
results support a complete design.
