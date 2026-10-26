# Hold a default-on daemon to today's Direct safety

- Date: 2026-09-26
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Keep the shared-browser daemon opt-in](2026-09-24-daemon-default-readiness.md)
- Status: contract decided; `daemon_enabled` stays `False` until the release gates below pass

## Decision

The shared browser may become the default once the stages demonstrate that it is no less safe than today's Direct default, scenario by scenario. Except for the two policy equivalences under Scope decisions, comparisons use the same user and host actions in both modes. The daemon must not produce:

1. an additional concurrent browser on a profile when Direct would not admit that browser;
2. a signal to a wrong target that Direct would not signal in the same scenario;
3. a profile mutation that Direct refuses;
4. a silent loss of a session that Direct keeps.

The owner launches, leases, guards and closes the browser through the same code as a Direct server. A hazard that both modes reach through the same path with the same consequence is shared: numeric `killpg` after an identity check ([#809](https://github.com/stickerdaniel/linkedin-mcp-server/issues/809)), Patchright's own kill paths, residual helpers after a crash, and cold inventory on Windows. Shared hazards are hardened for both modes and do not gate the default. Outside the two policy equivalences, a path that only the daemon takes to a shared primitive, or a worse consequence from that primitive, is in scope.

The earlier record asked the daemon for guarantees that today's default does not meet. Its reopening gates are answered below.

## Scope decisions

- **Clients.** Every client runs the newest release. Concurrent older versions are out of scope, and no rollback exists beyond `DAEMON_ENABLED=false` and `--no-daemon`. Linux, macOS and Windows switch together.
- **Storage.** Only local filesystems qualify. Local filesystem types are allow-listed and known sync providers are refused, including iCloud Drive, `~/Library/CloudStorage`, OneDrive, Dropbox roots from its `info.json`, and network drives. Sync the classifier cannot see, such as Syncthing or rsync, is a documented limitation.
- **Non-local roots.** A non-local, synced or unclassifiable auth root or daemon state root runs no daemon and keeps today's Direct behaviour with one warning. This exception exists because the documented Docker install bind-mounts `~/.linkedin-mcp` and NFS homes exist.
- **Custom browsers.** Only the bundled browser runs in default daemon mode. `CHROME_PATH` keeps today's Direct behaviour.
- **Dependencies.** The `patchright>=1.55.0` range stays open with no runtime version gate. Evidence is measured against the locked version and rerun on every lock change. Other versions, including a fresh `uvx` resolution, are a documented limitation. The limitation is narrower than it looks under this contract: Patchright's launch and kill paths are shared by both modes, so another resolved version changes Direct and the daemon alike, and the daemon-only modules do not import Patchright.
- **Profile commands under a live owner.** A `--logout`, `--login` or `--import-from-browser` command may request retirement only after the user confirms, and only when no tool call is in flight or queued. Once retirement wins, no new tool call is admitted. Cancelling before the request sends nothing. If the user cancels or the reply is lost after a confirmed request, the command reports that retirement may have begun and never claims the request was unsent. A busy owner causes a refusal without process IDs. The maintainer equates confirmed retirement of an idle owner with Direct after host quit. This is a policy equivalence, not a claim that every Direct command behaves the same way.
- **Browser that will not close.** After an unconfirmed close the owner releases the daemon lock and exits at once. It sends no signal itself. The crash guardian then performs the same marked drain as when a Direct host quits, and on Windows the per-launch Jobs run down at exit. The maintainer equates this automatic owner exit with Direct host quit. This is a policy equivalence, not a measured result. Under it, the guardian's marked-drain residual is shared hardening under #809, not a daemon-only path.

## Contract decided for implementation

- The guardian receives no owner group for an owner process, which matches a Direct server that does not lead its process group.
- When the owner cannot tell whether a process belongs to another Job it holds on Windows, it neither terminates that process in the routine drain nor declares the drain complete. The close then stays unconfirmed and keeps the profile lease until the owner-exit policy above applies. Job rundown and successor behaviour after that exit remain release evidence.
- The frontend never forwards an unmarked tool call, and the owner refuses unmarked calls. A successful, validated heartbeat preflight proceeds to a marked tool dispatch. Every failed status or invalid response is classified and dispatches no tool request, and its not-sent result refers to that tool request, not to the preflight exchange.
- Retirement and admission of new calls are decided on one tracker with no await between check and set. Idle exit, confirmed retirement, turnover and wedged exit all go through that gate.
- The stand-down route and its bearer check stay fixed across tool protocol changes and change only with the descriptor schema. A protocol mismatch yields a control-only attachment only after schema, endpoint, runtime, profile and token validation. That attachment may request turnover of an older owner and never runs a tool. Only a request with no body selects the legacy unconditional stand-down. A body that is malformed, unsupported, wrongly typed or addressed to another instance is rejected before any retirement state changes, and a failed idle-only request never falls back to unconditional stand-down. `PROTOCOL_VERSION` is bumped when call markers become mandatory.
- An owner with a different configuration but the same build is left alone, and the frontend falls back to Direct at once.
- An owner that implements this contract closes admission on turnover and lets in-flight calls run for up to 30 seconds before shutting down. A call cut off after that whose effect is unknown is classified as an unknown outcome and reported when a response can still be delivered. This is a policy to implement and test, not evidence of parity with Direct or a claim about how an older owner shuts down.
- Cancellation after client loss is an objective, not an exemption: the owner requests cancellation within the heartbeat expiry plus one poll of the last heartbeat it registered, absent owner stalls. The four outcomes apply to any work in that window.

## Release gates

1. A differential harness runs four experiments on Linux, macOS and Windows: repeatability, frozen Direct, negative controls against the current daemon, and the candidate, each with nonzero counts where it applies. Every negative control must reproduce its specified regression witness, labelled as source-model or native evidence, without reporting a hazardous path as a native wrong-target signal or session loss. The harness observes processes, signals and requests from outside the server processes, uses the account's real daemon state root keyed by a temporary auth root, and serves a synthetic origin that never contacts LinkedIn. Native runs that change a trust store happen only on disposable CI runners or machines.
2. A manual protocol on a Windows machine with Claude Desktop covers launch, four kinds of host or process loss, second host, profile commands, upgrade, non-local roots and suspend.
3. A pilot counts 60 daemon-mode sessions: 25 macOS, 20 Linux and 15 Windows. Fallback sessions are counted apart. The pilot installs the candidate the way users do, with a fresh `uvx` resolution, and records the Patchright version it resolved. Each session records the session state before and after. A loss is never excused by the owner's own expiry diagnosis. Every uncertain incident is resolved with evidence before the flip.
4. The flip is reviewed on the exact candidate with its lock, harness revision and executed case counts. Changing lifecycle code or the lock afterwards reruns the affected evidence.

## Earlier reopening gates

1. Version boundary and predecessor paths: closed by the client decision. Current CLI, opt-out and configuration boundaries are harness rows.
2. Identity-stable POSIX cleanup: under the automatic-exit equivalence, the remaining residual is shared hardening under #809. The implementation must remove the daemon-only owner sweep after an unconfirmed close; this record does not claim that removal is done.
3. Windows authority and real host: per-launch Job and cold inventory limits are shared. Named Job adoption, breakaway, the failed membership query and the real host are daemon evidence. The [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808) stop lifts only after the manual protocol passes.
4. Storage and delayed effects ([#796](https://github.com/stickerdaniel/linkedin-mcp-server/issues/796), [#821](https://github.com/stickerdaniel/linkedin-mcp-server/issues/821)): the storage decision settles which roots qualify. The stages must still show a total check that never raises and performs no daemon coordination read, write, lock or spawn for an ineligible root, keep the account-home protection checks, and verify that timed-out coordination work cannot later publish a stale generation. Locality alone closes neither the protection checks nor the delayed-effect evidence.
5. Packaged launch, host quit, cancellation, concurrency, opt-out: harness rows, the manual protocol and the pilot.

## Not established

No harness row, native measurement, manual step or pilot session has run under this contract. Driver exit after owner loss, Job rundown timing, trust-store support in the bundled browser and the other runtime claims behind this contract remain hypotheses until those gates measure them.
