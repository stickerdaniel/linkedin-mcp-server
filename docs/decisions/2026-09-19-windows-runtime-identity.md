# Name the Windows runtime without WMI

- Date: 2026-09-19
- Supersedes: none

The runtime id names a directory holding a browser runtime profile, and every
frontend and every daemon owner asks for it while starting.

On Windows it is built from `sys.platform` and from `PROCESSOR_ARCHITEW6432`
or `PROCESSOR_ARCHITECTURE`. Not from `platform.system()` or
`platform.machine()`: both of those are `platform.uname()`, and Windows
`uname()` has no `os.uname()` to read, so it fills every blank itself with
`win32_ver()` and `_get_machine_win32()`, a WMI query each. Either call pays
for both.

A WMI query can take a CPython 3.12 process down with it. The defect is in
3.12 only and will not be repaired there, so avoiding the query is the only
remedy this project has. `docs/windows-wmi-crash.md` holds the evidence.

The test is `sys.platform`, never `platform.system()`. Asking is the thing
being avoided, so asking in order to decide whether to avoid asking defeats
it. A first attempt at this fix avoided only `platform.machine()` and still
crashed for that reason.

There is deliberately no fallback past `PROCESSOR_ARCHITECTURE`. The only one
left would be the query this decision exists to avoid, so an unnamed
architecture is reported as unknown instead.

The id may not change value for an installation that already has one, or that
installation silently loses its runtime state. `_normalize_arch` maps the
environment values onto what the WMI reply mapped onto, which is what keeps
the two paths equal.
