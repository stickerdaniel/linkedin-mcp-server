# MCP Server for LinkedIn

A Model Context Protocol (MCP) server that connects AI assistants to LinkedIn. Access profiles, companies, and job postings through a Docker container.

> **Disclaimer:** This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.

## Features

- **Profile Access**: Get detailed LinkedIn profile information including experience, education, skills, projects, certifications, and more
- **Own Profile**: Fetch the authenticated user's own profile to give agents self-context
- **Profile Connections**: Send connection requests or accept incoming ones, with optional notes
- **Company Profiles**: Extract comprehensive company data, including the LinkedIn company URN id (used by LinkedIn's people-search `currentCompany` URL facet)
- **Company Employees**: List employees at a company with optional keyword filtering
- **Company Search**: Search for companies by keyword
- **Job Details**: Retrieve job posting information
- **Job Search**: Search for jobs with keywords and location filters
- **Saved Jobs**: List job postings saved by the authenticated user
- **People Search**: Search for people by keywords and location
- **Person Posts**: Get recent activity/posts from a person's profile
- **Company Posts**: Get recent posts from a company's LinkedIn feed
- **Home Feed**: Get recent posts from the authenticated user's LinkedIn home feed
- **Post Search**: Search posts/content globally by keyword (the "Posts" tab) with an optional recency filter
- **Compact References**: Return typed per-section links alongside readable text without shipping full-page markdown

## Quick Start

Docker includes full Chromium and an authenticated viewer for an explicit one-shot login. Create the host directory before mounting it so the unprivileged container user can write the session:

```bash
mkdir -p ~/.linkedin-mcp
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 127.0.0.1:6080:6080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --login --login-viewer
```

Open the complete loopback URL printed once by the command. The token is generated for that run, stored in a mode-0600 file, and carried in the URL fragment so the initial HTTP request remains token-free. Static noVNC files remain public; the token protects WebSocket control. Remote resize stays disabled, client-side scaling is fixed on, and the viewer closes after login, failure, a stop signal, or 1,800 seconds. Protected profile restoration may finish after remote control has closed, because interrupting a move on the mounted auth root could split the previous session. The command refuses to rotate any existing session unless the authentication root above the profile is on a writable, non-memory mount. If an older rootful Docker run created that host directory as root, repair it with `sudo chown -R "$(id -u):$(id -g)" ~/.linkedin-mcp`.

A profile created by the Docker viewer belongs to the container runtime and is reused directly on later Docker startups with the same runtime identity. A profile created on the host with `uvx mcp-server-linkedin@latest --login` or `--import-from-browser` belongs to a foreign runtime, so Docker derives a fresh Linux bridge from its source cookies on each startup.

**Configure Claude Desktop with Docker**

```json
{
  "mcpServers": {
    "mcp-server-linkedin": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "~/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "stickerdaniel/linkedin-mcp-server:latest"
      ]
    }
  }
}
```

> **Note:** Plain `--login` does not publish a viewer. Use `--login --login-viewer` only for the one-shot login container, with port 6080 published to loopback. The experimental shared-browser daemon is ignored in Docker because its owner can outlive the virtual display.
>
> **Note:** `stdio` is the default transport. Add `--transport streamable-http` only when you specifically want HTTP mode.
>
> **Note:** In HTTP mode the endpoint has no authentication, so the address it
> is published on is the only thing limiting who can use your LinkedIn session.
> Publish to loopback: `-p 127.0.0.1:8080:8080` together with
> `--host 0.0.0.0`. The wildcard host is required for the server to be reachable
> inside the container at all; the `127.0.0.1:` prefix on `-p` is what keeps it
> off your network. Without that prefix Docker publishes on every interface.
> Only expose it more widely behind something that authenticates.
>
> **Note:** Tool calls are serialized to protect the shared LinkedIn browser
> session, both within one server process and between separate ones. Only one
> process uses the browser at a time; others wait briefly and take over when it
> finishes a call. Use `LOG_LEVEL=DEBUG` to see the lock logs.
>
> **Note:** That coordination works between processes in the same runtime, but
> not between the host and a container sharing the mounted `~/.linkedin-mcp`
> directory. Do not run `--login` or `--logout` on the host while a container is
> running.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_DATA_DIR` | `~/.linkedin-mcp/profile` | Path to persistent browser profile directory. The container default is always usable; any other path needs a `profile-claim.json` marker in its parent, or one run with `--claim-profile-root` |
| `LOG_LEVEL` | `WARNING` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TIMEOUT` | `5000` | Browser timeout in milliseconds |
| `TOOL_TIMEOUT` | `180` | Per-tool MCP execution timeout in seconds. Increase further for heavy scrapes (multi-section profiles, cold-start Chromium, slow networks/containers). |
| `LOGIN_TIMEOUT` | `1800` | Manual login wait timeout in seconds (`0` = no ordinary limit). The Docker viewer caps the effective limit at its 1,800-second wall, so `0` becomes 30 minutes in viewer mode. Protected profile restoration may finish afterward. |
| `LOGIN_INLINE_WAIT` | `25` | Bounded inline wait (seconds, max 45) for a tool call to resume after login completes. No effect in normal container server mode; the explicit `--login --login-viewer` command is the Docker login path. |
| `BROWSER_WAIT` | `25` | How long (seconds, max 45) to wait for another server process to hand over the shared browser before reporting that it is busy. `0` reports busy immediately. |
| `BROWSER_MIN_HOLD` | `20` | Shortest time (seconds) a process keeps the shared browser before handing it to a waiting process. Higher means fewer browser restarts but longer waits for other clients; clamped below `BROWSER_WAIT` so a waiting client is served before its own timeout. `0` hands over after every tool call. |
| `BROWSER_IDLE_TIMEOUT` | `600` | Close an idle browser and release the shared profile after this many seconds without a tool call. `0` keeps it open until the server exits. |
| `AUTO_IMPORT_FROM_BROWSER` | on by default | Auto-import a LinkedIn session from a locally logged-in browser on the first no-session tool call, before falling back to manual login. On by default across interactive and non-interactive desktop runs; set `false` to require `--login` / `--import-from-browser`. No effect in containers (no host browser or keychain) or on a non-loopback HTTP bind. On macOS the OS keychain may prompt once for Safe Storage access. |
| `TRANSPORT` | `stdio` | Transport mode: stdio, streamable-http |
| `HOST` | `127.0.0.1` | HTTP server host (for streamable-http transport) |
| `PORT` | `8000` | HTTP server port (for streamable-http transport) |
| `HTTP_PATH` | `/mcp` | HTTP server path (for streamable-http transport) |
| `SLOW_MO` | `0` | Delay between browser actions in ms (debugging) |
| `HUMAN_DELAYS` | `false` | Wait a random interval between browser actions instead of a fixed cadence. Off by default, and off means unchanged timing. Reduces the chance of tripping LinkedIn's throttling; not a guarantee against it |
| `HUMAN_DELAY_MIN_SECONDS` | `1.0` | Shortest randomized wait, in seconds |
| `HUMAN_DELAY_MAX_SECONDS` | `5.0` | Longest randomized wait, in seconds (maximum `30`) |
| `HEADLESS` | `false` | Docker defaults to full headed Chromium on its virtual display. Set `true` only to deliberately use Chromium's real headless mode, which identifies itself as `HeadlessChrome`. |
| `DAEMON_ENABLED` | `false` | The experimental shared-browser daemon is ignored in Docker. Its owner is designed to outlive a stdio frontend, while the virtual display belongs to that frontend's process group. |
| `VIEWPORT` | `1280x720` | Browser viewport size as WIDTHxHEIGHT. Docker is headed by default and therefore uses its real Xvfb window size; this applies only when `HEADLESS=true`. |
| `CHROME_PATH` | - | Path to Chrome/Chromium executable (rarely needed in Docker) |
| `PROXY_SERVER` | - | Optional, and most setups are better off without one: LinkedIn advises against proxies and scores the addresses a session signs in from, so a stable known address beats a commercial exit node. Worth it when the container runs somewhere its address is obviously a data centre, and even then a WireGuard or Tailscale exit node on your own network is preferable. Route the browser through a proxy, as `scheme://host:port` (`http`, `https`, `socks4`, `socks5`). May also carry credentials directly (`http://user:pass@host:port`), which is how most providers hand them out. Inside a container `127.0.0.1` is the container itself: for a relay running on the host use `host.docker.internal` (on native Linux Docker, add `--add-host=host.docker.internal:host-gateway`). Only browser traffic is routed, not the MCP transport. |
| `PROXY_USERNAME` | - | Username for the proxy |
| `PROXY_PASSWORD` | - | Password for the proxy. Env-only by design: there is no CLI flag, because command-line arguments are readable by every user on the machine. Chromium cannot authenticate to a SOCKS proxy, so credentials require an `http(s)` endpoint. |
| `PROXY_BYPASS` | - | Comma-separated hosts to reach directly instead of through the proxy |
| `LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION` | `false` | Experimental: reuse checkpointed derived Linux runtime profiles across Docker restarts instead of fresh-bridging each startup |
| `LINKEDIN_TRACE_MODE` | `on_error` | Trace/log retention mode: `on_error` keeps ephemeral artifacts only when a failure occurs, `always` keeps every run, `off` disables trace persistence |

**Example with custom timeouts:**

```json
{
  "mcpServers": {
    "mcp-server-linkedin": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "~/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "-e", "TIMEOUT=10000",
        "-e", "TOOL_TIMEOUT=300",
        "stickerdaniel/linkedin-mcp-server"
      ]
    }
  }
}
```

## Repository

- **Source**: <https://github.com/stickerdaniel/linkedin-mcp-server>
- **License**: Apache 2.0
