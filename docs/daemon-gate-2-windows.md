# Gate 2: the Windows protocol with Claude Desktop

Release gate 2 of the shared-browser daemon, run by hand on one Windows machine.
How each step is read (the four losses, the upgrade, the bundle) is decided in
[the gates 2 and 3 decision record](decisions/2026-10-15-daemon-gates-2-and-3-procedure.md);
this runbook holds the commands. The
[#808](https://github.com/stickerdaniel/linkedin-mcp-server/issues/808) stop
lifts only once every area below passes.

## Prerequisites

- A Windows 11 machine with Claude Desktop, signed in, and Windows Terminal.
- [uv](https://docs.astral.sh/uv/) installed. `(Get-Command uvx).Source` prints
  the absolute `uvx.exe` path the config needs.
- The candidate commit SHA from the maintainer.
- [Process Explorer](https://learn.microsoft.com/sysinternals/downloads/process-explorer)
  for area 8.
- A OneDrive folder synced on this machine and an SMB share you can map, for area 6.
- No other MCP host using this LinkedIn profile.

Set these once per PowerShell window. The cache directory must not exist yet:
one fresh `uvx` resolution serves the whole protocol. Keep `$Dir` free of
spaces, since `$Candidate` is passed on as one string.

```powershell
$Sha = "<candidate sha>"
$Dir = "C:\linkedin-gate2"
$Cache = "$Dir\cache-$Sha"
$Evidence = "$Dir\evidence"
$From = "git+https://github.com/stickerdaniel/linkedin-mcp-server@$Sha"
$Candidate = "uvx --isolated --cache-dir $Cache --from $From mcp-server-linkedin"
$env:LINKEDIN_TRACE_MODE = "always"
function Server { uvx --isolated --cache-dir $Cache --from $From mcp-server-linkedin @args }
function Recorder { uvx --isolated --cache-dir $Cache --from $From python "$Dir\daemon_pilot.py" @args }
```

Get the recorder from the same commit, then sign in once:

```powershell
New-Item -ItemType Directory -Force $Dir | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/stickerdaniel/linkedin-mcp-server/$Sha/scripts/daemon_pilot.py" -OutFile "$Dir\daemon_pilot.py"
Server --login
Server --status
```

`--status` must end with `Session is valid`.

### Claude Desktop config

Claude Desktop reads `%APPDATA%\Claude\claude_desktop_config.json` and writes
each server's stderr to `%APPDATA%\Claude\logs\mcp-server-<name>.log`
([MCP docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers)).
Settings, Developer, Edit Config opens the file. Use absolute paths and quit
Desktop completely after every change.

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "C:\\Users\\<you>\\.local\\bin\\uvx.exe",
      "args": [
        "--isolated", "--cache-dir", "C:\\linkedin-gate2\\cache-<sha>",
        "--from", "git+https://github.com/stickerdaniel/linkedin-mcp-server@<sha>",
        "mcp-server-linkedin",
        "--daemon", "--log-level", "INFO", "--browser-idle-timeout", "600"
      ],
      "env": { "LINKEDIN_TRACE_MODE": "always" }
    }
  }
}
```

The `.mcpb` bundle has no daemon setting, so it is not used here.

### Finding the processes

```powershell
# Frontends: the Python process under uvx.exe under claude.exe.
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'mcp-server-linkedin|linkedin_mcp_server' } |
  Select-Object ProcessId, ParentProcessId, CreationDate, Name, CommandLine | Format-List

# The owner: its command line runs -m linkedin_mcp_server.daemon.owner.
$Owner = Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match '-m linkedin_mcp_server\.daemon\.owner' }
$OwnerId = $Owner.ProcessId
$OwnerStart = (Get-Process -Id $OwnerId).StartTime

# Browsers on the profile: main processes carry no --type switch.
Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" |
  Where-Object { $_.CommandLine -like '*--user-data-dir=*' -and $_.CommandLine -notlike '*--type=*' } |
  Select-Object ProcessId, ParentProcessId, CreationDate
```

Capture the owner's PID and start time while you watch it start. Before any
kill, check it is still the same process; never use `started_at` from
`daemon.json` as a creation time.

```powershell
if ((Get-Process -Id $OwnerId).StartTime -ne $OwnerStart) { throw "PID reused" }
Stop-Process -Id $OwnerId -Force
```

### Evidence for every step

Wrap each step in the recorder, with the step's name as the host:

```powershell
Recorder before --pilot-dir $Evidence --host "gate2-<area>" --others-stopped --candidate $Candidate
# ... the step ...
Recorder after --pilot-dir $Evidence --session <id>
```

`after` waits up to 20 minutes for the owner's idle exit and copies the owner
log window and the trace runs. Once it has finished, copy
`%APPDATA%\Claude\logs\mcp*.log` into the session folder; `after` refuses a
folder that already holds anything the recorder did not write. Note each action's
wall-clock time. Trace runs are pruned after 14 days, so copy evidence out the
same day.

## Areas

Each area passes when every observation matches. Anything else is a finding,
recorded with its evidence.

### 1. Launch

1. `before`, then start Claude Desktop.
2. Ask Claude to read your own LinkedIn profile.
3. Run the process listing.

Expected: one frontend under `uvx.exe` under `claude.exe`, one owner, one
browser main process. Its trace run has the version banner then
`Forwarding to the shared browser owner`. `daemon.log` has the tool's
`Reading own profile` line. The read returns.

4. Quit Desktop, then `after`. Pass: mode `daemon`, outcome `retained`.

### 2. The four losses

Each starts from a cold `before` and a started Desktop, and ends with `after`.
For a call in flight, ask for a slow read such as a full person profile with
every section, and act while Desktop shows it running.

1. **Host quits normally.** Quit Desktop while a call runs.
2. **Host killed.**
   ```powershell
   $Claude = Get-CimInstance Win32_Process -Filter "Name = 'claude.exe'"
   $Root = $Claude | Where-Object { $_.ParentProcessId -notin $Claude.ProcessId }
   Stop-Process -Id $Root.ProcessId -Force
   ```
3. **Frontend killed, host alive.** `Stop-Process -Id <frontend python pid> -Force`
   during a call. Desktop stays open; ask for another read.
4. **Owner killed with a call in flight, a second call armed (#808).** Start a
   second host (area 3) with the same command. Start a slow read in Desktop,
   then a read in the second host, so both wait on the owner. Revalidate and
   kill the owner. Record the three times.

Expected for all four: never two browser main processes on the profile at
once, and none left within a minute of its owner's exit. The owner outlives
losses 1 to 3 until its idle exit. The interrupted call ends with an error or
an unknown outcome, never a silent success. The next read works in a new host
session, and `after` reports `retained`. For loss 4, the log timestamps show
the second call armed before the kill, and that call either completes through
a replacement owner or reports the profile busy.

### 3. Second host

1. `before`, start Desktop and a second stdio host (for example Claude Code in
   Windows Terminal) with the same command, args and env.
2. Read from each host in turn, then from both at once.

Expected: two frontends, both logging `Forwarding to the shared browser owner`,
one owner PID, one browser main process. Quitting one host leaves the other
working.

### 4. Profile commands in Windows Terminal

Gate 1 left these to this protocol because Windows has no pseudo-terminal in
the harness. Run each in Windows Terminal while the owner is idle (Desktop
quit, within 10 minutes of its last read), in this order. `$Flags` keeps the
commands on the host's settings:

```powershell
$Flags = "--daemon", "--log-level", "INFO"
```

1. **Decline.** `Server @Flags --logout`, answer `y` to
   `Are you sure you want to clear the profile?`, then `N` to
   `Ask it to retire and continue?`. Expected: exit 0, the same owner PID and
   start time, no `A profile command asked for the browser; standing down` in
   `daemon.log`, and `Server --status` still valid once the owner has exited.
2. **Logout.** `Server @Flags --logout`, answer `y` to both. Expected: the
   owner logs the stand-down line and exits, then the session is cleared.
3. **Login.** `Server --login` to sign in again, then start Desktop, read and
   quit. `Server @Flags --login`, answer `y`, sign in. Expected: the owner
   retires before the login opens the profile, and `Server --status` is valid
   afterwards.
4. **Busy owner.** With a slow read in flight in Desktop, run
   `Server @Flags --logout`, `Server @Flags --login` and
   `Server @Flags --import-from-browser`, one per call, confirming every
   prompt. Expected: each exits 1 and names no process, nothing is deleted or
   rotated, no browser starts, and the read then completes.

Areas 4.2 and 4.3 change the session on purpose, so their `after` records an
incident by design; note that in the evidence.

### 5. Upgrade (blocked)

Blocked until a released predecessor contains the clean-slate namespace and the
candidate's installed version is strictly newer. v4.27.0 does not qualify, and
a locally bumped build never counts. Once unblocked:

1. Run Desktop on the predecessor
   (`--from mcp-server-linkedin==<version>`, its own fresh cache) with the same
   args, read, and quit Desktop while its owner is idle.
2. Switch the config to the candidate and start Desktop.

Expected: the frontend logs
`The running daemon is version <old> and this server is <new>; asking it to hand the browser over`,
the old owner logs `A newer build asked for the browser; standing down`, and
the read works through the new owner with the session retained.

### 6. Non-local roots

Add `"USER_DATA_DIR"` to the config's `env` as an absolute path: first the one
`"$env:OneDrive\linkedin-gate2\profile"` prints, then one on a mapped share
(`net use Z: \\<server>\<share>`, `Z:\linkedin-gate2\profile`). Neither
Desktop nor the server expands `%OneDrive%`. For each, pass the same path to
the recorder, so it reads the trace runs under that root, then start Desktop
and read:

```powershell
Recorder before --pilot-dir $Evidence --host gate2-non-local --others-stopped --candidate $Candidate --user-data-dir <the same absolute path>
```

The status there has no session, so the record does not count as a pilot
session; it is kept as evidence.

Expected: the frontend's trace run logs `The ... is on ... storage (...);
this server drives its own browser instead of sharing one`, and
`%USERPROFILE%\.linkedin-mcp-daemon\daemon` gains no directory. Remove
`USER_DATA_DIR` afterwards.

### 7. Suspend

With a call in flight, put the machine to sleep from Start, Power, Sleep. Wake
it after at least two minutes.

Expected: the call completes or ends with an error or an unknown outcome;
`daemon.log` may say `The owner was not scheduled for ...; nobody is written off for that`.
The next read works and `after` reports `retained`.

### 8. Desktop Job topology

During area 1, open Process Explorer, then each process's Properties, Job tab:
`claude.exe`, the frontend, the owner, and the browser main process. Save a
screenshot of each.

Expected: the owner runs in its own Job named `linkedin-mcp-owner-...`, not in
any Job of Claude Desktop, and keeps running after Desktop quits until its idle
exit. Record Desktop's Job name and limits as found.

## Completion

Gate 2 passes when areas 1 to 8 pass on one candidate SHA, area 5 included.
Hand in the evidence folder, the Desktop logs and the screenshots for the gate
review, together with the Windows build, the Claude Desktop version and the
candidate SHA.
