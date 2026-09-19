# Evidence for stable Linux process-group signals

Issue #809 identifies a check-to-signal race in numeric process-group cleanup.
This change records only the first evidence level. It does not select a new
production backend and does not change cleanup behavior on Linux or macOS.

## L1: synthetic native group

`tests/test_pidfd_group_signal.py` starts a dedicated Linux helper, makes it a
child subreaper with `PR_SET_CHILD_SUBREAPER`, and creates a private session
whose leader spawns one member. The helper opens both pidfds while the processes
are alive and their identities are directly observable. It then reaps the
leader, proves that the member still belongs to that group, and sends `SIGUSR1`
through the retained leader pidfd with `PIDFD_SIGNAL_PROCESS_GROUP`. The
member's signal handler supplies independent delivery evidence.

The subreaper explicitly collects the reparented member with `waitpid` before
the test expects the retained leader pidfd to report `ESRCH` for the empty
group. The member pidfd remains open until cleanup finishes. Every cleanup
signal uses `pidfd_send_signal(member_pidfd, SIGKILL, None, 0)`; `ESRCH` means
the member is already gone. No numeric PID or PGID signal is used. Both pidfds
are closed and checked for `EBADF` before the helper exits.

The flag is `1UL << 2` in Linux
[`include/uapi/linux/pidfd.h`](https://github.com/torvalds/linux/blob/master/include/uapi/linux/pidfd.h).
The test pins its numeric value and probes the operation against the private
group. The member pidfd is opened before this probe, so every result after
spawn already has identity-stable cleanup. `EINVAL` means that the running
kernel does not support the group flag; flag-specific `ENOSYS` or `EPERM` means
the group operation is unavailable. Those paths skip only after the leader and
member have both been pidfd-signaled as needed, explicitly reaped, and their
pidfds closed. Deterministic fault injection exercises all three paths. Missing
Python APIs and failures of the flags-zero preflight skip before any group is
spawned. No skip substitutes numeric `killpg`. A separate call with an unknown
nonzero bit checks that the available Python API passes flags to Linux rather
than accepting and discarding them.

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
