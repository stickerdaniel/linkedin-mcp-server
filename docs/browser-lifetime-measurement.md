# Browser lifetime measurement

Measured 2026-10-25 on native Linux amd64. This is a bounded, synthetic comparison
of upstream `7a40310c6beb77e7e2cea8c649f3411f96b65f94` and the implementation in
`c4f55df97bd47f7ba6051eaa64d89c1a601b8986`. It measures the memory/latency tradeoff
of `--browser-max-calls`; it does not reproduce LinkedIn's heap or session behavior.

## Method

All arms used the published upstream 4.28.0 image at
`sha256:22f0c7a426b98a9d8a9165f99e24bfb58d615ca4f704dd0c45a153b19f5b0fed`,
with the respective source overlaid read-only. The dependency lock is identical:
Python 3.13.13, FastMCP 4.0.10, Patchright 1.63.0 and its managed Chrome for Testing
153.0.8010.12. Each run had a fresh temporary home/profile, headed Xvfb, no external
network, 1 GiB memory with swap disabled, 512 MiB shared memory, 512 PIDs and two CPUs.
There were no live profiles, cookies, accounts or LinkedIn requests.

A real FastMCP client called a synthetic tool through the actual sequential tool
middleware, driver singleton, cross-process profile lease, guardian and
`BrowserManager`. The only authentication substitution was a loopback title check
in place of the feed-auth validator, with an owned empty portable-session record.
Launch, reopen and close behavior was unchanged. This exercises direct browser
ownership, not detached-owner election, forwarding or concurrent clients.

Each arm ran 16 calls, repeated three times in a balanced serial order:

- **Navigation:** navigate to a new loopback URL and read 3,000 synthetic paragraph
  rows per call, without deliberately retaining buffers.
- **Retained state:** stay on one page and allocate/touch 8 MiB of JS buffers per
  call, in the same isolated execution world. This intentionally artificial
  retention resets with each browser generation.

The arms were upstream/default zero, PR/default zero and PR/cap four. Real browser
generations and feed-validation stand-ins were counted. Whole-cgroup
`memory.current` and memory categories were sampled every 50 ms; the kernel peak,
OOM events and call attempts were also retained. This includes browser, Node,
Xvfb, guardian, client and temporary profile memory, rather than renderer RSS
alone. No host cache was dropped.

## Results

Each row contains three successful runs. Peak columns are sampled peaks in MiB;
roundtrip is the sum of the 16 successful tool calls, including middleware close
when due, excluding inter-call pauses and final out-of-call cleanup.

| Workload | Source / cap | Peak cgroup MiB, median (range) | Peak anon MiB, median | Total tool roundtrip s, median | Browser generations |
| --- | --- | ---: | ---: | ---: | ---: |
| Navigation | Upstream / 0 | 781.2 (779.2–791.2) | 720.6 | 2.69 | 1 |
| Navigation | PR / 0 | 823.6 (781.4–831.4) | 765.1 | 2.75 | 1 |
| Navigation | PR / 4 | 583.9 (576.8–585.6) | 519.5 | 5.76 | 4 |
| Retained state | Upstream / 0 | 606.2 (604.7–607.7) | 552.1 | 1.21 | 1 |
| Retained state | PR / 0 | 606.3 (606.3–607.4) | 552.4 | 1.20 | 1 |
| Retained state | PR / 4 | 488.2 (479.6–488.6) | 427.1 | 4.09 | 4 |

All 288 calls completed. Every cap-four run used four real browser generations;
both default-zero arms used one. Each replacement performed its own validation
stand-in. Every run proved native close, absence of surviving launch-marked
processes and profile-lease release. All 18 containers exited zero without an OOM
kill, OOM-event delta or sampler error.

Closing after four calls lowered sampled peak memory in both fixtures and added
startup/validation latency. Three repeats do not establish default-zero memory
equivalence: the navigation arms show substantial variation. Shared file-cache
charging and scheduling also affect these whole-cgroup values. The retained-state
fixture demonstrates release of process-lifetime state, rather than evidence of
a LinkedIn leak or an estimate of its memory savings. No arm intentionally crossed
the final memory limit, so this is not a demonstrated OOM-prevention threshold.

## Evidence limits and processing

An earlier qualification navigation failed with smaller memory, shared-memory and
PID budgets (768 MiB / 128 MiB / 256). Its cause was not established, and it is
excluded from the table; it cannot establish an OOM or a PR regression. All counted
runs used the common budgets above. Authentication, live session compatibility and
a suitable production call cap remain unmeasured; the default remains zero.

The executed harness SHA-256 was
`e4b338b71b6745a24154309c1a9baeeea62106e6afd33cef6f8ee6ea2bca63ce`.
Its initial latency aggregate had a field-name collision with cumulative sampler
time. The table's timings were derived from the separately recorded successful
attempt intervals in the original raw files, with one ordered interval per call
and enclosing-clock checks. Two independent checks verified that correction;
memory, counts and cleanup evidence were unchanged. Original records and a
hash-linked derivation manifest were retained. The browser measurements were not
rerun or represented as runs of a later corrected harness.
