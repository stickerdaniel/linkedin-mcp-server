# Windows browser launch evidence

Status: native evidence passed; production implementation stopped

Date: 2026-09-22

Issue: #808

## Decision

Production integration remains stopped. Before changing the Windows profile-fence protocol, collect one native measurement against the exact locked Patchright stack. The measurement launches a real, network-isolated persistent Chromium context in a named inner Job while the coordinator, launch owner, and guardian remain in an outer harness Job. The guardian retains stable handles for a quiescent two-sided CDP and Job census, keeps a real profile lease through Job zero and retained-handle zero, and releases it only after the coordinator permits release.

The native CI result is authoritative. Stack, topology, inventory, and fence mismatches fail rather than skip or rewrite the evidence.

## Acceptance boundary

The evidence requires the locked Python package, bundled core, Chromium revision and version, wheel-owned Node runtime, browser executable, browser-level version, CDP version, browser and renderer roles, three pre-release lease rejections, and outer harness cleanup proof. Renderer and worker execution are local `page.evaluate` activity with a Blob-backed worker. There is no network navigation.

## Non-claims

This stage does not establish a production protocol, choose lock bytes, change defaults, prove that `TerminateJobObject` caused every observed exit, cover anonymous production Job ownership, or authorize production integration. It does not measure LinkedIn, authentication, a real profile, proxy behavior, browser identity evasion, or any user session. Conditional GPU, utility, and crashpad processes are inventory observations, not acceptance premises.

## Native result

GitHub Actions run [35765288361](https://github.com/stickerdaniel/linkedin-mcp-server/actions/runs/35765288361) passed the Windows `platform-behaviour` leg on Python 3.13 at commit `887d4339cfe3df0fabb2df0c2a8532b1ac6a86b0`. The passing test established the locked Patchright, Node, libuv, and Chromium stack; Node-only prelaunch membership; guardian arming before browser launch; browser and renderer containment; a browser image matching the resolved executable; local renderer and worker execution; three lease rejections; release only after Job and handle proof; and an outer Job containing only the coordinator's ancestor chain.

The Actions step summary holds the raw inventory. This record does not transcribe conditional GPU, utility, or crashpad observations from that summary. Production integration remains stopped.
