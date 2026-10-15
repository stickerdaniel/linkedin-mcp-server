# Read release gates 2 and 3 of the shared-browser daemon

- Date: 2026-10-15
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Hold a default-on daemon to today's Direct safety](2026-09-26-daemon-default-on-contract.md), only where its gates 2 and 3 leave a choice open
- Status: decided; `daemon_enabled` stays `False` until all four release gates pass

## Decision

This record decides how gates 2 and 3 are read where the contract leaves a choice. Gate 1 passed on `f39a2893` ([measurements](../daemon-measurements.md#release-gates)). Commands, paths and host setup belong to the runbooks, which change as hosts change: [gate 2](../daemon-gate-2-windows.md) and [the pilot](../daemon-pilot.md).

## The four losses (gate 2)

The four kinds of host or process loss are named by what the tester does, not by what the transport does underneath:

1. the host quits normally;
2. the host process is killed;
3. the frontend process is killed while the host lives;
4. the owner is killed with a call in flight, while a second host's call is already armed to contend for the profile.

The fourth one is the race in [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808).

Pipe loss remains harness evidence (H-R4-pipe, native on Windows in gate 1) and is not taken to show how Claude Desktop ends its server. A browser crash is a Patchright path both modes share, so it is not one of the four.

## Claude Desktop (gate 2)

Gate 2 runs Claude Desktop from `claude_desktop_config.json` with the candidate installed through `uvx`. The bundle has no daemon setting, so gate 2 cannot reach it in daemon mode. The flip review (gate 4) therefore needs a launch of the exact candidate's `.mcpb` that has been executed, not just named.

## Upgrade (gate 2)

An upgrade needs two installed versions, both verified at install: a released predecessor that contains the [clean-slate](2026-10-11-daemon-clean-slate.md) namespace, and a candidate whose version is strictly newer. v4.27.0 predates that namespace. Until a qualifying release exists, the upgrade step is blocked. A build whose version was bumped locally does not count.

## Mode of a session (gate 3)

Mode is decided once for each frontend process, from its startup decision.

- **Daemon.** The frontend logs that it is forwarding to the shared owner.
- **Fallback.** The frontend logs that it drives its own browser.
- **Uncertain.** The frontend logs neither.

A proxy that later fails to recover an owner stays in daemon mode, with a failure. A host session's mode follows this precedence:

1. **Uncertain** if any of its frontends is uncertain.
2. **Fallback** if, otherwise, any frontend falls back.
3. **Daemon** only when every frontend in it is daemon.

## Counted sessions and their outcome (gate 3)

The pilot counts cold-start host sessions. Each one must:

- start with no owner running and no other host open;
- start from a session that `--status` validated beforehand;
- contain at least one LinkedIn-reading tool call that the owner logged on entry.

Owner reuse across host sessions is evidence from gates 1 and 2, not from the pilot. A session in which the tester logs in, logs out or imports does not qualify. Its record is kept, and a loss in it stays an incident.

A counted session is either:

- **retained:** validated again afterwards, with no unexplained change to its identity; or
- **an incident:** anything else, including a failed or unverified validation, an unreadable store, a quarantine, or a new login generation.

A tester's account of what they did is evidence and never closes an incident. A later login never resolves an earlier loss, and neither does the owner's own expiry diagnosis. Incidents are closed only in the gate review, with evidence. The recorder never closes one.

## Not decided here

The choice of pilot testers, the order in which gates 2 and 3 run, and gate 4.
