# Runbook: a native crash in the Windows daemon soak

How to get a symbolised native stack out of the `election-soak` workflow when a
process dies with an access violation instead of a Python traceback. Written
for #838; every measurement below is from `windows-latest`, 2026-09-19, unless
it names another date.

## When you need this

`faulthandler` cannot describe some Windows faults. If its report is complete —
every thread printed as `Thread 0x…` — and yet no thread is marked
`Current thread 0x…`, then the thread that faulted holds no Python thread
state. `faulthandler_dump_traceback` takes the current thread from
`PyGILState_GetThisThreadState()` and `_Py_DumpTracebackThreads` marks a thread
only on pointer equality with it, so an unmarked complete dump means that call
returned `NULL`. No Python-level report can name that thread. Only a native
stack can.

## Prerequisites

**`SEM_NOGPFAULTERRORBOX` must be clear in the faulting process.** This is the
one that mattered. While it is set, `UnhandledExceptionFilter` returns at once:
no Windows Error Reporting, no `AeDebug` debugger, no `Application Error` event,
and the process exits carrying the exception code. Measured as `0x0003` in the
test frontend, inherited from somewhere above pytest; nothing in this repository
sets it. `tests/test_daemon_election.py` clears that one bit and leaves the
rest, and only under `LINKEDIN_MCP_ELECTION_SOAK`, because clearing it turns
crash reporting back on and only the soak workflow arms the no-dialog setting
that makes that safe.

**A postmortem debugger must be registered**, because the process that dies is a
grandchild: the test starts eight frontends and each starts a daemon of its own,
so no workflow step can attach to the one that will fault, and which one it is
only becomes known once it is gone. `procdump -i` registers under `AeDebug`,
which Windows takes on an unhandled exception.

That registration is the capture route, not one of two. Microsoft documents
that LocalDumps is skipped when an automatic debugger is configured, so the
`Arm a crash dump` step's `DumpFolder`, `DumpCount` and `DumpType` do not
describe what actually lands: ProcDump is installed with `-ma` and writes
full-memory dumps, which are large and should be treated as potentially
sensitive rather than assumed to hold only stacks. Keep that step anyway — it
sets `DontShowUI`, which is what keeps a fault from waiting for a click, and it
is the fallback if the registration is ever removed.

One observation worth recording without over-reading it: run 35367449209 caught
the access violation with LocalDumps armed and uploaded nothing. WER was
disabled on that runner, but the process error mode was also suppressing crash
reporting, so that run does not isolate which of the two was responsible.

`procdump` is downloaded at run time and then registered as the debugger
Windows starts on any unhandled exception, so the step checks its Authenticode
signature is valid and from Microsoft before running it.

That is publisher trust, not artifact pinning, and the two are not equivalent.
A digest would reject any changed bytes; the signature check accepts whatever
Microsoft signs next, including a version that behaves differently from the one
that collected a given dump. The trade is deliberate — Sysinternals republishes
the same URL, so a pin breaks the workflow on every refresh — but if a capture
ever has to be reproduced exactly, record the accepted executable's version and
digest from the run that produced it.

## Capture

Dispatch `election-soak` with enough rounds that a crash is likely; at the rate
observed before the fix, roughly one in 40 rounds. A crashing round fails the
step, so the `Upload the crash dump` artifact is produced.

## Symbolise

On macOS, no Windows machine needed:

```bash
brew install msitools sevenzip
cargo install dump_syms minidump-stackwalk   # lands in ~/.cargo/bin
```

Fetch the matching debug symbols and build a Breakpad store. The Python version
has to match the runner's exactly:

```bash
curl -O https://www.python.org/ftp/python/3.12.10/amd64/core_pdb.msi
curl -O https://www.python.org/ftp/python/3.12.10/amd64/exe_pdb.msi
msiextract core_pdb.msi exe_pdb.msi
dump_syms --store symbols/store <extracted>/python312.pdb
```

Then walk each dump:

```bash
minidump-stackwalk --human \
  --symbols-path symbols/store \
  --symbols-url https://symbols.mozilla.org/ \
  --output-file stack.txt \
  python.exe_*.dmp
```

`--symbols-url` supplies the Microsoft system libraries; the local store
supplies CPython's own.

## Completion criteria

You are done when the output names a module and an instruction, not just
addresses. For #838 that was:

```
EXCEPTION_ACCESS_VIOLATION_READ
Thread 7 (crashed)  _wmi.pyd + 0x1650
  mov rcx, qword [rsi + 0x8]    rsi = 0x0000002b20dbb878
```

Cross-check against the Windows event log, which the `Report what the dump
folder holds` step prints:

```
Faulting module name: _wmi.pyd, version: 3.12.10150.1013
```

If the stack is all addresses, the symbol store does not match the runner's
Python build. Check the debug id — for 3.12.10 amd64 it is
`BCE8A026179B4DF78F3A5EB76C30A4491` — before assuming anything about the fault.
