# Windows browser launch evidence

Status: native evidence pending

Date: 2026-09-22

Issue: #808

## Decision

Production integration remains stopped. Before changing the Windows profile-fence protocol, collect one native measurement against the exact locked Patchright stack. The measurement launches a real, network-isolated persistent Chromium context in a named inner Job while the coordinator, launch owner, and guardian remain in an outer harness Job. The guardian retains stable handles for a quiescent two-sided CDP and Job census, keeps a real profile lease through Job zero and retained-handle zero, and releases it only after the coordinator permits release.

The native CI result is authoritative. Stack, topology, inventory, and fence mismatches fail rather than skip or rewrite the evidence.

## Acceptance boundary

The evidence requires the locked Python package, bundled core, Chromium revision and version, wheel-owned Node runtime, browser executable, browser-level version, CDP version, browser and renderer roles, three pre-release lease rejections, and outer harness cleanup proof. The page uses only `about:blank` and `set_content`; a retained Blob-backed worker proves renderer and worker execution without network navigation.

## Non-claims

This stage does not establish a production protocol, choose lock bytes, change defaults, prove that `TerminateJobObject` caused every observed exit, cover anonymous production Job ownership, or authorize production integration. It does not measure LinkedIn, authentication, a real profile, proxy behavior, browser identity evasion, or any user session. Conditional GPU, utility, and crashpad processes are inventory observations, not acceptance premises.

The record may be updated with the native CI measurement after this commit runs on Windows. Until then, native evidence is pending and production remains stopped.
