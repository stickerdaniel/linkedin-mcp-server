# The Windows WMI crash behind #838

Why a WMI query can kill a CPython 3.12 process on Windows, and what was
measured. The decision that follows from it is
[`docs/decisions/2026-09-19-windows-runtime-identity.md`](decisions/2026-09-19-windows-runtime-identity.md).

## The defect

Source: CPython `PC/_wmimodule.cpp`, read at tag `v3.12.10`.

`_wmi_exec_query_impl` places `struct _query_data data = {0}` on its own stack
and hands `&data` to a `CreateThread` worker, which keeps the pointer:

```c
struct _query_data *data = (struct _query_data*)param;
```

The caller then waits 1000ms for COM initialisation, 100ms for the connection
and 100ms for the thread. On `WAIT_TIMEOUT` it closes the handles and returns
anyway, under the comment `Probably stuck - there's not much we can do,
unfortunately`. The worker is still running, and reads through a stack frame
that no longer exists.

Fixed upstream by copying the struct into the worker: commit `e4fbfb128`,
*GH-130727: Avoid race condition in _wmimodule by copying shared data
(GH-134313)*, 2025-05-20.

Branch containment, measured 2026-09-19 with `git branch --contains e4fbfb128`
against a CPython clone:

| Branch | Has the fix |
| --- | --- |
| 3.12 | no |
| 3.13 | yes |
| 3.14 | yes |
| main | yes |

A branch is not a release. The fix reached 3.13 users in **3.13.4**, released
2025-06-03, so 3.13.0 through 3.13.3 carry the defect as well. Read the table
as the current state of those branches, not as a property every release on them
has always had.

3.12 is in security-only maintenance, so no backport has come. That is a
statement about today rather than a guarantee about tomorrow, and calling this
a crash is not a finding that it could never be a vulnerability. The split the
table shows is the one #838 reports.

## The captured fault

Measured 2026-09-19 on `windows-latest`, CPython 3.12.10, symbolised with
`minidump-stackwalk` against PDBs from python.org (`python312.pdb`, debug id
`BCE8A026179B4DF78F3A5EB76C30A4491`):

```
EXCEPTION_ACCESS_VIOLATION_READ
Thread 7 (crashed)  _wmi.pyd + 0x1650
  mov rcx, qword [rsi + 0x8]    rsi = 0x0000002b20dbb878
```

`rsi` points into a thread stack, not into the heap. The Windows event log
named the same module:

```
Faulting application name: python.exe, version: 3.12.10150.1013
Faulting module name: _wmi.pyd, version: 3.12.10150.1013
```

## Why no report ever named it

Two separate reasons, and both had to be removed before anything was visible.

The faulting thread is a raw `CreateThread` worker with no Python thread
state. `faulthandler_dump_traceback` takes `PyGILState_GetThisThreadState()`
and passes it to `_Py_DumpTracebackThreads`, which marks the current thread by
pointer equality alone. When that call returns `NULL`, the dump is complete and
no thread is marked. A complete report with no `Current thread` line is
therefore the signature of a thread Python does not know about, and that is
what every #838 report showed.

No crash dump existed either, because the process error mode had
`SEM_NOGPFAULTERRORBOX` set, measured as `0x0003` in the frontend and
inherited from above pytest. That bit makes `UnhandledExceptionFilter` return
immediately: no WER, no `AeDebug`, no `Application Error` event, and the
process simply exits carrying the exception code. Nothing in this repository
sets it. Clearing that one bit in the test frontend is what produced the dump
above.

## The load that reaches it

Contention is the trigger observed here, not the only way to exhaust those
waits: the same 3.12 source discusses a five-second delay when the caller
lacks permission to connect, which alone exceeds them. What this test supplies
is load. The eight-client election starts eight frontends at once, each
spawning a daemon owner, so sixteen processes initialise COM together, and
every one of them asks for the runtime id.

## Verification of the fix

Measured 2026-09-19 on `windows-latest`, CPython 3.12.10:

| Tree | Rounds | Access violations |
| --- | --- | --- |
| before the fix | 7 runs of 80 | 6 runs crashed |
| first attempt, avoiding only `platform.machine()` | 4 runs of 80 | rounds 2, 5 and 6 |
| the fix | 254 | 0 |

About a dozen crashes were expected across 254 rounds at the observed rate.

`platform.uname` was also instrumented and the frontend's exact import set run
against it, including dependencies: 0 calls during imports, 0 calls from
`get_runtime_id()` on the Windows branch, and `platform._uname_cache` still
`None` afterwards.

## Alternatives

Avoiding the query does not mean the architecture is unobtainable. Windows
answers it without WMI through `IsWow64Process2`, whose separate native-machine
output preserves ARM64 under x86 and x64 emulation. This project uses that
output before consulting the compatibility environment. On Windows versions
too old to export that API, `GetNativeSystemInfo` remains a fallback for x86
and AMD64 only; see
the decision record for the residual boundary.
