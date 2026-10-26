# Keep the shared-browser daemon opt-in

- Date: 2026-09-24
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Status: default enablement stopped; no production protocol selected

## Decision

Keep `daemon_enabled = False` on every platform. This review does not authorize an `auto` mode or a new admission or cleanup protocol. An election failure followed by Direct fallback is not proof that the previous browser has stopped. A platform-limited default would be a separate product decision, not completion of cross-platform readiness.

This decision was checked against source at `9f2cc9c410792f5fae3d4facba6868721c291ce9`. Closing the election and startup-log issues [#1014](https://github.com/stickerdaniel/linkedin-mcp-server/issues/1014) and [#801](https://github.com/stickerdaniel/linkedin-mcp-server/issues/801) did not establish the remaining crash, compatibility, or filesystem guarantees.

## Established boundaries

- [Windows region inversion](2026-09-21-windows-region-inversion-evidence.md) was falsified for a byte-zero-only entrant. The [real browser launch measurement](2026-09-22-windows-browser-launch-evidence.md) passed its native harness, but explicitly stopped production integration. Neither test proves a production crash fence or a real Claude Desktop Job topology.
- The [Linux pidfd L1 experiment](2026-09-19-pidfd-process-group-evidence.md) used a synthetic group. It did not capture the locked Patchright browser launch or establish macOS cleanup. Numeric `killpg` after a separate identity check still has the wrong-target interval tracked in [#809](https://github.com/stickerdaniel/linkedin-mcp-server/issues/809).
- `daemon_liveness.py` deliberately accepts calls without a recognized marker and does not cancel them after client death. That mixed-version behavior does not meet a universal killed-client guarantee. An idle-owner exit without a real Chromium launch is not browser-drain evidence.

A passing native harness, a clean unit suite, and an opt-in deployment each have narrower scope than default-on approval. No new browser, host, predecessor, filesystem-failure, or soak measurement is claimed by this record.

## Reopening gates

Before a default-on plan can be approved, establish and review:

1. A supported caller/owner version boundary, released predecessor artifacts, and actual behavior on both sides of upgrade, including [#810](https://github.com/stickerdaniel/linkedin-mcp-server/issues/810), [#818](https://github.com/stickerdaniel/linkedin-mcp-server/issues/818), and [#819](https://github.com/stickerdaniel/linkedin-mcp-server/issues/819). Old Direct, Login, and Import entry paths must not bypass any new profile fence.
2. Identity-stable POSIX cleanup for the real browser tree, with safe refusal when the required primitive is unavailable. A numeric-PGID fallback or acceptance of an existing wrong-target window is not a safety argument.
3. Windows owner/guardian authority through handle transfer, browser ARM/DISARM, every protected mutation, isolated and combined holder losses, uncertain drain, and a real host's ambient Job. Preserve exclusion until independently verified drain. The Windows [#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808) production stop remains in force.
4. Bounded, identity-safe admission for both coordination state and the configured authentication root, including inaccessible or remote storage ([#796](https://github.com/stickerdaniel/linkedin-mcp-server/issues/796)) and the supported Windows home policy ([#821](https://github.com/stickerdaniel/linkedin-mcp-server/issues/821)). A timeout around a blocked publication must not leave a stale writer free to publish later.
5. Actual packaged-launcher and host-quit behavior, real killed-client cancellation and browser teardown, concurrent-client operation, and rollback that does not reopen unsafe Direct admission. Only after these gates pass can a separately reviewed, explicitly scoped default change and pilot be considered.

Unknown or failed evidence leaves the daemon opt-in. This record is not a migration runbook and does not authorize deleting a lock, tombstone, state directory, or profile to force progress.
