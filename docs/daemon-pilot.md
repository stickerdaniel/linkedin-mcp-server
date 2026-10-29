# Gate 3: the daemon pilot

Release gate 3 of the shared-browser daemon counts 60 daemon-mode host
sessions: 25 on macOS, 20 on Linux and 15 on Windows. Each tester records their
sessions with `scripts/daemon_pilot.py` from the candidate commit. Which
sessions count and what makes an incident is decided in
[the gates 2 and 3 decision record](decisions/2026-10-15-daemon-gates-2-and-3-procedure.md).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) installed, and one MCP host such as Claude
  Desktop, Claude Code or Cursor.
- The candidate commit SHA from the maintainer.
- A LinkedIn session made with the candidate (below).
- Nothing else using the profile: one host at a time, every other host quit.

## Install the candidate

Pick a cache directory that does not exist yet. That directory is the
candidate's fresh `uvx` resolution; reuse it for every session of this SHA.

macOS and Linux:

```bash
SHA=<candidate sha>
DIR="$HOME/linkedin-pilot"
CACHE="$DIR/cache-$SHA"
PILOT="$DIR/sessions"
FROM="git+https://github.com/stickerdaniel/linkedin-mcp-server@$SHA"
CANDIDATE="uvx --isolated --cache-dir '$CACHE' --from $FROM mcp-server-linkedin"
server() { uvx --isolated --cache-dir "$CACHE" --from "$FROM" mcp-server-linkedin "$@"; }
recorder() { uvx --isolated --cache-dir "$CACHE" --from "$FROM" python "$DIR/daemon_pilot.py" "$@"; }
mkdir -p "$DIR"
curl -fsSLo "$DIR/daemon_pilot.py" \
  "https://raw.githubusercontent.com/stickerdaniel/linkedin-mcp-server/$SHA/scripts/daemon_pilot.py"
server --login
```

With a clone, `git show $SHA:scripts/daemon_pilot.py` gives the same file.
The recorder runs on the candidate's own interpreter, so it reads the package
and Patchright versions that resolution installed. The variables and functions
live in this shell; set them again in a new one.

Windows (PowerShell). Keep the directory free of spaces: `--candidate` is
one string, and Windows PowerShell 5.1 drops quotes nested inside it.

```powershell
$Sha = "<candidate sha>"
$Dir = "C:\linkedin-pilot"
$Cache = "$Dir\cache-$Sha"
$Pilot = "$Dir\sessions"
$From = "git+https://github.com/stickerdaniel/linkedin-mcp-server@$Sha"
$Candidate = "uvx --isolated --cache-dir $Cache --from $From mcp-server-linkedin"
function Server { uvx --isolated --cache-dir $Cache --from $From mcp-server-linkedin @args }
function Recorder { uvx --isolated --cache-dir $Cache --from $From python "$Dir\daemon_pilot.py" @args }
New-Item -ItemType Directory -Force $Dir | Out-Null
Invoke-WebRequest "https://raw.githubusercontent.com/stickerdaniel/linkedin-mcp-server/$Sha/scripts/daemon_pilot.py" -OutFile "$Dir\daemon_pilot.py"
Server --login
```

Keep the pilot directory outside `~/.linkedin-mcp` and `~/.linkedin-mcp-daemon`;
the recorder refuses one inside either. Leave the session folders as the
recorder writes them: `after` refuses a folder that holds a link or anything
the recorder did not write.

## Host config

Every host runs the same command with these arguments and environment. Use
the absolute path of `uvx` (`command -v uvx`, or `(Get-Command uvx).Source` on
Windows) and of the cache directory.

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "/absolute/path/to/uvx",
      "args": [
        "--isolated", "--cache-dir", "/absolute/path/to/cache-<sha>",
        "--from", "git+https://github.com/stickerdaniel/linkedin-mcp-server@<sha>",
        "mcp-server-linkedin",
        "--daemon", "--log-level", "INFO", "--browser-idle-timeout", "600"
      ],
      "env": { "LINKEDIN_TRACE_MODE": "always" }
    }
  }
}
```

`--log-level INFO` and `LINKEDIN_TRACE_MODE=always` are what let the recorder
see the session's mode and tool calls. Without them a session is `uncertain`.
Claude Desktop's config is `claude_desktop_config.json`
([MCP docs](https://modelcontextprotocol.io/docs/develop/connect-local-servers));
the `.mcpb` bundle has no daemon setting and does not count.

## One session

1. Quit every MCP host.
2. Record the start (`Recorder` and `$Pilot` on Windows):
   ```bash
   recorder before --pilot-dir "$PILOT" --host claude-desktop --others-stopped --candidate "$CANDIDATE"
   ```
   It prints the session ID and `Start the host now`. If it says the session
   does not count, stop here; the record is kept anyway. If it refuses because
   an owner may still be running, wait ten minutes and try again.
3. Start the host and use LinkedIn through it as you normally would, with at
   least one read (a profile, the feed, a search). Do not log in, log out or
   import in this session, and run no other server command until `after`:
   the recorder counts every server process it sees as part of the session,
   and one it cannot place makes the session uncertain.
4. Quit the host completely.
5. Record the end:
   ```bash
   recorder after --pilot-dir "$PILOT" --session <session id>
   ```
   It waits up to 20 minutes for the shared browser's idle exit, then prints
   the session's mode and outcome. If you did log in, log out or import,
   say so: `--attest login@2026-10-15T14:05`, once per command. If `after`
   is interrupted, run the same command again: it keeps what it captured
   and repeats only the final status check. One interrupted while it was
   still capturing records the session as an incident, and so does a
   capture changed since.

Run `recorder tally --sha "$SHA" "$PILOT"` to see your counts. It counts
only this candidate's sessions, and exits non-zero until the whole pilot
passes, which is expected for one tester.

## Send back

Zip the session folders under the pilot directory and send them to the
maintainer privately. Each holds `before.json`, `after.json`, and a
`capture` folder with `capture.json`, `daemon.log` and `trace-runs/`.

The logs contain tool arguments: profile names, search keywords, message
recipients. Read them first and redact what you do not want to share. Never
send `cookies.json`, `source-state.json`, the browser profile, or anything else
from `~/.linkedin-mcp`; the recorder never copies them.

## Completion

The maintainer runs `daemon_pilot.py tally --sha <candidate sha>` over every
received pilot directory. Gate 3 passes when it exits 0: every quota met,
no incident and no uncertain session open. The tool never closes an incident;
the gate review does, with evidence.
