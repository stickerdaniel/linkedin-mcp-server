# Measure Windows Job topology before integration

- Date: 2026-09-21
- Issue: [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808)
- Status: native synthetic topology evidence passed; production implementation stopped

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

## Native result

GitHub Actions run
[`35613835424`](https://github.com/stickerdaniel/linkedin-mcp-server/actions/runs/35613835424)
executed all four topology scenarios successfully on Windows with Python 3.12.4,
3.13 and 3.14 at commit `4c5c877bc3b996128a4ec2229ed2b560ed7e6de5`.
Each run observed the topology actor in the outer Job before its start gate,
required ordinary children to remain in both known Jobs, accepted guardian
creation only under the scenario-specific containment contract, and required
all three common-ancestor members to be live before termination and exit with
code 204 afterward. Platform-independent tests separately cover result
classification, Popen provenance, membership acceptance, error preservation and
scenario routing.

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
