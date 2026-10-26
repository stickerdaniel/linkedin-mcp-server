# Start the shared-browser daemon from a clean slate

- Date: 2026-10-11
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Hold a default-on daemon to today's Direct safety](2026-09-26-daemon-default-on-contract.md), only where its contract prescribes behaviour toward released daemon versions: the bodyless request as the legacy unconditional stand-down, and a stand-down route that changes with the descriptor schema
- Status: decided; `daemon_enabled` stays `False` until the release gates of the superseded record pass

## Decision

The daemon has only ever shipped opt-in, and nobody is known to run it. The maintainer decided on 2026-10-11 that no compatibility with any released daemon version is kept. Every path that existed only for an earlier release is removed. The version handling that remains is what this release needs so that a later one can retire it.

Everything else in the superseded record stands unchanged: the four outcomes the daemon must not produce compared with Direct (an additional concurrent browser on a profile, a signal to a wrong target, a profile mutation Direct refuses, a silent loss of a session Direct keeps), the scope decisions, the rest of the contract decided for implementation, the release gates and the earlier reopening gates.

## What changes

- **Fresh state namespace.** Daemon state lives under `.linkedin-mcp-daemon` in the account home on every platform. Earlier releases used `.mcp-server-linkedin` and, on Windows, `.mcp-server-linkedin-v2`. This build never reads, creates, replaces or removes either, so an owner from an earlier release never shares coordination state with it. Browser exclusion against such an owner is still the profile lock that Direct uses as well. The account-home pinning, restricted creation and access-list checks apply to the new namespace.
- **Versions reset.** `SCHEMA_VERSION` and `PROTOCOL_VERSION` start again at 1 in the new namespace. Call markers are mandatory from protocol 1. Nothing collides, because no earlier descriptor is read here.
- **One startup protocol.** The frontend's configuration record names startup protocol 4, and an owner refuses a record with any other value or with none before it touches any state. The number is not reset to 1: released frontends sent 1 (by leaving the field out), 2 and 3, and the released owners that read the field accept only those, so a new number makes a startup across that boundary fail on both sides. The earliest owners never read the field and refuse today's record only because its configuration carries fields they do not know; a startup handshake that no earlier owner speaks is the general fence, and replaces the loopback channel in a later stage. Commit authorization runs over the loopback control channel only. The owner takes the daemon lock itself on every platform; no lock is handed to it.
- **Removed.** The pipe-carried commit and the bare ready verdict of earlier startup protocols, lock handoff by inherited descriptor, the Windows tombstone and the wait for its exclusion, and the bodyless stand-down request.

## The stable interface

One request is kept fixed for as long as this release can be resident: the one a later release sends to retire it.

- `POST /control/stand-down` on the endpoint the descriptor names, with the bearer token beside that descriptor.
- A JSON body of exactly `{"version": 1, "mode": "turnover" | "idle_only", "instance": "<instance id>"}`. `turnover` closes admission and drains in-flight calls; `idle_only` retires only an owner with nothing in flight or queued, as a confirmed profile command needs.
- Anything else, including a request with no body, is answered 400 before any retirement state changes. A failed idle-only request is never retried as a turnover.

The request version is independent of the tool protocol and of the descriptor schema. A later release that changes either must still read a schema 1 descriptor far enough to address this request, or it cannot retire an owner of this release.

A control-only attachment remains for the two cases that are not compatibility: a same-build owner with another configuration, which is probed once and left alone, and a future tool protocol mismatch, which may only send this request.
