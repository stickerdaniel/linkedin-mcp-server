# Name the Windows runtime without WMI

- Date: 2026-09-19
- Supersedes: none

The runtime id names a directory holding a browser runtime profile, and every
frontend and every daemon owner asks for it while starting.

On Windows it is built from `sys.platform` and from `PROCESSOR_ARCHITEW6432`,
`PROCESSOR_ARCHITECTURE`, or `GetNativeSystemInfo`, in that order. Not from
`platform.system()` or `platform.machine()`: both of those are
`platform.uname()`, and Windows `uname()` has no `os.uname()` to read, so it
fills every blank itself with `win32_ver()` and `_get_machine_win32()`, a WMI
query each. Either call pays for both.

A WMI query can take a CPython 3.12 process down with it, and that defect will
not be repaired there, so avoiding the query is the only remedy this project
has. `docs/windows-wmi-crash.md` holds the evidence.

The test is `sys.platform`, never `platform.system()`. Asking is the thing
being avoided, so asking in order to decide whether to avoid asking defeats
it. A first attempt at this fix avoided only `platform.machine()` and still
crashed for that reason.

## Why the kernel is asked last, and asked at all

`platform._get_machine_win32` queries WMI *first* and reads the two variables
only when that fails. So a process whose environment lacks them — a service or
an MCP host that sanitises what it passes down — used to be given an
architecture by the query. Answering `unknown` there would rename the
directory holding that installation's runtime profile, and the session inside
it would stop being found. `GetNativeSystemInfo` closes that gap without WMI:
`Win32_Processor.Architecture` and `SYSTEM_INFO.wProcessorArchitecture` are one
enumeration, so the table CPython indexes with the WMI reply reads the kernel's
answer unchanged.

It is asked last rather than first so that no process which already had an
answer gets a new one. That is every ordinary Windows process.

## What this preserves, and what it does not

For the architectures Windows actually reports, the environment spelling and
the WMI spelling normalise to the same name, WOW64 included, because
`PROCESSOR_ARCHITEW6432` carries what the query would have said.

One case is not established: an x64 process under emulation on an ARM64 host,
where the environment says `AMD64`. `GetNativeSystemInfo` is documented to
report `AMD64` there too, for compatibility, while what the WMI query returned
has not been measured. If that pairing ever needs to be exact,
`IsWow64Process2` reports the native machine separately and is also WMI-free.
Nothing here should be read as a promise that every emulation combination
produces the identity it produced before.
