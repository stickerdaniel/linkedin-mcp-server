# Turn over an older owner whatever its configuration

- Date: 2026-10-12
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Start the shared-browser daemon from a clean slate](2026-10-11-daemon-clean-slate.md), only where it names the cases a control-only attachment remains for
- Status: decided; `daemon_enabled` stays `False` until the release gates of the 2026-09-26 contract pass

## Decision

A frontend asks an older owner to stand down whatever that owner's configuration fingerprint. The owner must first be proved for control: descriptor schema, runtime, profile, endpoint and token. An owner that fails any of those checks is never asked.

Unchanged:

- An owner of the same version with another configuration is probed once and left alone, and the frontend falls back to Direct.
- A newer owner, or one whose version cannot be compared, is never asked. With another configuration the frontend waits on the lock.
- An owner of another tool protocol keeps its own rule: asked when older, left running otherwise.

"Older" and "same version" compare package versions. They do not compare build identity, because an editable install or a rebuild keeps its version.

## Why

Before this, a newer frontend retired an older owner when the fingerprints matched and when the tool protocols differed, but not when only the fingerprint differed. Every change to the fingerprint's fields then left an older owner running against every newer frontend. That bought no safety: the control-only pair is exactly as proved as an attachable one, and the turnover request is the same bounded drain either way.

## Scope

A frontend attached to the retired owner with that owner's other configuration gets no continuity: its recovery cannot attach to the successor. Concurrent older frontends are outside the contract's client scope.

## Evidence

`tests/test_daemon_election_core.py::test_every_look_keeps_the_contract` requires an active pass to ask every older owner it decides on, and `tests/test_daemon_composed_core.py` runs the upgrade across configurations through every bounded schedule.
