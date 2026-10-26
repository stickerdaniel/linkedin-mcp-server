# Scope release gate 1 of the shared-browser daemon

- Date: 2026-10-14
- Issue: [#606](https://github.com/stickerdaniel/linkedin-mcp-server/issues/606)
- Supersedes: [Hold a default-on daemon to today's Direct safety](2026-09-26-daemon-default-on-contract.md), only where its release gate 1 asks for signals to be observed from outside the server processes on macOS and Windows
- Status: decided; `daemon_enabled` stays `False` until all four release gates pass

## Decision

The maintainer decided on 2026-10-14 what release gate 1 requires of the differential harness. Everything else in the contract stands, including the four outcomes, gates 2 to 4 and the [clean-slate](2026-10-11-daemon-clean-slate.md) changes.

## Signals

Linux is the only platform where the harness traces signals natively (`strace` on selected actors). On macOS and Windows it records signals as unobserved. Gate 1 accepts that: on those two platforms, wrong-target signals (outcome 2) rest on the source-model witnesses, and gate 1 makes no native claim of parity with Direct there. Two rows that are both unobserved are not evidence of equal signalling; the comparison already says so.

## Negative controls

"The current daemon" in gate 1 means the frozen pre-fix revision the harness pins (`BASELINE_SHA` in `tests/differential/baseline.py`). A witness counts only when it fails on its intended assertion at that pin, never on exit status alone.

## Not native in gate 1

Each of these keeps the coverage it has and is named in the evidence record rather than counted as a native cell:

- **The unconfirmed close on macOS (H-R7).** The row reads the marked drain's aftermath with a signal tracer, which macOS lacks, so it follows from the signals decision. Linux runs it natively, and its judgement has browser-free tests in `tests/differential/test_unconfirmed_close.py`.
- **Profile commands that need a terminal, on Windows.** Those cells need a pseudo-terminal, which Windows lacks without a new dependency, and the gate 2 manual protocol covers them on a real Windows host. The cells on pipes, `--status` and the no-terminal cell, stay native there.
- **Import from a browser into an idle owner.** The import asks the OS keystore before it reads a cookie on every platform, so a disposable synthetic profile cannot show it natively anywhere. Its coverage is `test_import_waits_for_the_profile_after_an_idle_owner_retires` in `tests/test_cli_main.py`, counted under H-R10a-login in `tests/differential/model_coverage.py`.

## Required before gate 1 passes

- **A response-loss witness.** No test yet drops a marker response on the owner hop, so a session lost that way is unmeasured. Gate 1 needs a source-model test that drops it at the hop boundary and shows the caller is not left with a silent loss Direct would not produce. Until that test exists, gate 1 does not pass.

## Evidence

One dispatch of `.github/workflows/differential-evidence.yml` with `platform=all` and `group=all` on the exact candidate settles the gate; a narrower dispatch does not. It is reviewed cell by cell, by row, platform, architecture, experiment and column, with node identifiers and skip reasons, and each witness is checked on its own. The candidate SHA, lock hash, harness revision, run identifiers and attempts, runner images and counts go into `docs/daemon-measurements.md`, and the packets are archived with checksums before the workflow's artifacts expire. A retry does not replace an unexplained failure. A change to lifecycle code, the harness or the lock after that dispatch reruns the evidence it affects.
