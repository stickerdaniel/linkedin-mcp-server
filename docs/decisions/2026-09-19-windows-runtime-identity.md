# Name the Windows runtime without WMI

- Date: 2026-09-19
- Supersedes: none

The runtime id names a directory holding a browser runtime profile, and every
frontend and every daemon owner asks for it while starting.

On Windows it is built from `sys.platform` and the native-machine output of
`IsWow64Process2`, followed by `PROCESSOR_ARCHITEW6432` and
`PROCESSOR_ARCHITECTURE` only when the native API cannot answer. Not from
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

## Why the kernel is asked first

`platform._get_machine_win32` queries WMI *first* and reads the two variables
only when that fails. The replacement has to preserve that authority as well as
avoid WMI: under x64 emulation on ARM64, `PROCESSOR_ARCHITECTURE` can say AMD64
while `Win32_Processor.Architecture` said ARM64. Reading the environment first
would silently abandon the existing ARM64 runtime profile.

`IsWow64Process2` supplies the native machine separately from the process
compatibility architecture. Its `IMAGE_FILE_MACHINE` values are mapped to the
spellings CPython 3.12 used for the WMI enumeration. The environment remains a
fallback when the native API cannot answer.

On Windows versions where `IsWow64Process2` is unavailable, the fallback reads
`GetNativeSystemInfo` only for x86 and AMD64. Those Windows releases predate
Windows on ARM64; other answers become `unknown` rather than extending a
compatibility result into a claim about the native machine.

## What this preserves, and what it does not

For ordinary processes, the native API and the old WMI spelling normalise to
the same name. Under x86 or x64 emulation on ARM64, the `nativeMachine` output
wins over both the `processMachine` output and a compatibility architecture in
the environment.

The residual boundary is failure of the native API. On Windows older than
`IsWow64Process2`, `GetNativeSystemInfo` preserves x86 and AMD64 identities and
other architectures fall through to the environment or `unknown`. Those
releases predate Windows on ARM64. On a newer system where the API exists but
fails, the environment remains the last available answer and may itself
contain a compatibility architecture.
