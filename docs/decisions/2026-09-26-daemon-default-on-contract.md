# Hold a default-on daemon to today's Direct safety

- Date: 2026-09-26
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Keep the shared-browser daemon opt-in](2026-09-24-daemon-default-readiness.md)
- Status: contract decided; `daemon_enabled` stays `False` until the release gates below pass

## Decision

The shared browser may become the default once it is no less safe than today's Direct default, scenario by scenario. For every failure scenario, with the same user and host actions in both modes, the daemon must not produce:

1. a second browser on one profile where Direct has none;
2. a signal to a wrong target where Direct sends none;
3. a profile mutation that Direct refuses;
4. a silent loss of a session that Direct keeps.

The owner launches, leases, guards and closes the browser through the same code as a Direct server. A hazard that both modes reach through the same path with the same consequence is shared: numeric `killpg` after an identity check ([#809](https://github.com/stickerdaniel/linkedin-mcp-server/issues/809)), Patchright's own kill paths, residual helpers after a crash, and cold inventory on Windows. Shared hazards are hardened for both modes and do not gate the default. A path that only the daemon takes to a shared primitive is in scope.

The earlier record asked the daemon for guarantees that today's default does not meet. Its reopening gates are answered below.

## Scope decisions

- **Clients.** Every client runs the newest release. Concurrent older versions are out of scope, and no rollback exists beyond `DAEMON_ENABLED=false` and `--no-daemon`. Linux, macOS and Windows switch together.
- **Storage.** Only local filesystems qualify. Local filesystem types are allow-listed and known sync providers are refused, including iCloud Drive, `~/Library/CloudStorage`, OneDrive, Dropbox roots from its `info.json`, and network drives. Sync the classifier cannot see, such as Syncthing or rsync, is a documented limitation.
- **Non-local roots.** A non-local, synced or unclassifiable auth root or daemon state root runs no daemon and keeps today's Direct behaviour with one warning. This exception exists because the documented Docker install bind-mounts `~/.linkedin-mcp` and NFS homes exist.
- **Custom browsers.** Only the bundled browser runs in default daemon mode. `CHROME_PATH` keeps today's Direct behaviour.
- **Dependencies.** The `patchright>=1.55.0` range stays open with no runtime version gate. Evidence is measured against the locked version and rerun on every lock change. Other versions, including a fresh `uvx` resolution, are a documented limitation.
- **Profile commands under a live owner.** A confirmed `--logout`, `--login` or `--import-from-browser` may retire an owner only after the user confirms, and only while the owner has no tool call in flight or queued. Nothing is sent before confirmation or after a cancel. A busy owner is refused like a Direct server, without process IDs. This treats an idle owner like Direct after host quit.
- **Browser that will not close.** After an unconfirmed close the owner releases the daemon lock and exits at once. It sends no signal itself. The crash guardian then performs the same marked drain as when a Direct host quits, and on Windows the per-launch Jobs run down at exit. This treats automatic owner exit like Direct host quit. The residual is the guardian's shared marked drain, hardened under #809, not a daemon-only path.

## Contract decided for implementation

- The guardian receives no owner group for an owner process, which matches a Direct server that does not lead its process group.
- A Windows Job membership query that fails never leads to termination; the close stays unconfirmed and keeps the profile lease.
- The frontend never forwards a call unmarked. The owner refuses unmarked calls. A failed heartbeat preflight is reported as not sent, and every HTTP status has a classified, non-dispatching outcome.
- Retirement and admission of new calls are decided on one tracker with no await between check and set. Idle exit, confirmed retirement, turnover and wedged exit all go through that gate.
- The stand-down route and its bearer check stay fixed across tool protocol changes and change only with the descriptor schema. A protocol mismatch yields a control-only attachment used to turn over an older owner, never to run a tool. `PROTOCOL_VERSION` is bumped when call markers become mandatory.
- An owner with a different configuration but the same build is left alone, and the frontend falls back to Direct at once.
- A turnover requested by a newer frontend lets in-flight calls finish for up to 30 seconds. A call still running after that is reported as an unknown outcome.
- Cancellation after client loss is an objective, not an exemption: the owner requests cancellation within the heartbeat expiry plus one poll of the last heartbeat it registered, absent owner stalls. The four outcomes apply to any work in that window.

## Release gates

1. A differential harness runs four experiments on Linux, macOS and Windows: repeatability, frozen Direct, the current daemon, which must show its known regressions, and the candidate. It observes processes, signals and requests from outside the server processes, uses the account's real daemon state root keyed by a temporary auth root, and serves a synthetic origin that never contacts LinkedIn. Native runs that change a trust store happen only on disposable CI runners or machines.
2. A manual protocol on a Windows machine with Claude Desktop covers launch, four kinds of host or process loss, second host, profile commands, upgrade, non-local roots and suspend.
3. A pilot counts 60 daemon-mode sessions: 25 macOS, 20 Linux and 15 Windows. Fallback sessions are counted apart. Each session records the session state before and after. A loss is never excused by the owner's own expiry diagnosis. Every uncertain incident is resolved with evidence before the flip.
4. The flip is reviewed on the exact candidate with its lock, harness revision and executed case counts. Changing lifecycle code or the lock afterwards reruns the affected evidence.

## Earlier reopening gates

1. Version boundary and predecessor paths: closed by the client decision. Current CLI, opt-out and configuration boundaries are harness rows.
2. Identity-stable POSIX cleanup: shared hardening under #809. The daemon-only owner sweep after an unconfirmed close is removed.
3. Windows authority and real host: per-launch Job and cold inventory limits are shared. Named Job adoption, breakaway, the failed membership query and the real host are daemon evidence. The [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808) stop lifts only after the manual protocol passes.
4. Storage and delayed effects ([#796](https://github.com/stickerdaniel/linkedin-mcp-server/issues/796), [#821](https://github.com/stickerdaniel/linkedin-mcp-server/issues/821)): closed by the storage decision and a total eligibility check that never raises and never touches coordination state for an ineligible root.
5. Packaged launch, host quit, cancellation, concurrency, opt-out: harness rows, the manual protocol and the pilot.

## Not established

No harness row, native measurement, manual step or pilot session has run under this contract. Driver exit after owner loss, Job rundown timing, trust-store support in the bundled browser and the other runtime claims behind this contract remain hypotheses until those gates measure them.
