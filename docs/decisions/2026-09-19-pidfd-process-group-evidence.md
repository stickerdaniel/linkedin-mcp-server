# Evidence for stable Linux process-group signals

Issue #809 identifies a check-to-signal race in numeric process-group cleanup.
This change records only the first evidence level. It does not select a new
production backend and does not change cleanup behavior on Linux or macOS.

## L1: synthetic native group

`tests/test_pidfd_group_signal.py` creates a private Linux session whose leader
spawns one member. The test opens the leader pidfd while the leader is alive and
its identity as the group leader is directly observable. It then reaps the
leader, proves that the member still belongs to that group, and sends `SIGUSR1`
through the retained pidfd with `PIDFD_SIGNAL_PROCESS_GROUP`. The member's signal
handler supplies independent delivery evidence. The same test checks that a
member pidfd is rejected, an empty group returns `ESRCH`, and every pidfd is
closed.

The flag is `1UL << 2` in Linux
[`include/uapi/linux/pidfd.h`](https://github.com/torvalds/linux/blob/master/include/uapi/linux/pidfd.h).
The test pins its numeric value and probes the operation against the private
group. `EINVAL` means that the running kernel does not support the operation;
the probe skips and never substitutes numeric `killpg`. A separate call with an
unknown nonzero bit checks that the supported Python API passes flags to Linux
rather than accepting and discarding them.

## Evidence not claimed

This test does not force the dead leader's numeric PID and PGID to be reused.
Doing that deterministically needs CI that can create a private user and PID
namespace, mount a private procfs, and control PID allocation through
`/proc/sys/kernel/ns_last_pid`. The namespace owner must have
`CAP_CHECKPOINT_RESTORE` or `CAP_SYS_ADMIN` for that control, and the harness
must run as the namespace's PID 1 or install a subreaper so every generated
process is collected. The current unprivileged test matrix does not guarantee
those facilities. PID churn on the shared runner would be timing-dependent and
would not be evidence of reuse safety.

A locked Patchright real-launch probe is the next evidence level, not part of
L1. It must run with the repository's locked browser available, without a login,
and must observe the actual browser leader early enough to open its pidfd before
exit. Until that topology and timing are stable in CI, a synthetic browser
launch would only repeat the process-group test above under a different name.
