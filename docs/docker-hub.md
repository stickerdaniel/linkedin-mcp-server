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

Create a browser profile locally, then mount it into Docker. You still need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed on the host for the one-time `uvx mcp-server-linkedin@latest --login` step. Docker already includes its own Chromium runtime, so the managed Patchright Chromium browser download used by MCPB/`uvx` is not needed here.

**Step 1: Create profile on the host (one-time setup)**

```bash
uvx mcp-server-linkedin@latest --login
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The browser profile and cookies are saved under `~/.linkedin-mcp/`. On startup, Docker derives a Linux browser profile from your host cookies and creates a fresh session each time. For better stability, consider the [uvx setup](https://github.com/stickerdaniel/linkedin-mcp-server#-uvx-setup-recommended---universal).

> **Already signed into LinkedIn in a browser on the host?** Run `uvx mcp-server-linkedin@latest --import-from-browser` on the host to reuse that session instead of `--login`. It supports Chrome, Chromium, Brave, Edge, Arc, Vivaldi, Helium, Yandex, and Naver Whale, auto-picks the most recently used browser with a live LinkedIn session (pass a browser name to target one), writes the same `~/.linkedin-mcp/` profile Docker mounts, and the Docker bridge still narrows to the minimal auth cookie subset it uses for a normal session. Cookies under Chrome 127+ app-bound encryption cannot be imported; use `--login` in that case.

**Step 2: Configure Claude Desktop with Docker**

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

> **Note:** Docker containers don't have a display server, so you can't use the `--login` command in Docker. Create a source profile on your host first.
>
> **Note:** `stdio` is the default transport. Add `--transport streamable-http` only when you specifically want HTTP mode.
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
| `USER_DATA_DIR` | `~/.linkedin-mcp/profile` | Path to persistent browser profile directory |
| `LOG_LEVEL` | `WARNING` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TIMEOUT` | `5000` | Browser timeout in milliseconds |
| `TOOL_TIMEOUT` | `180` | Per-tool MCP execution timeout in seconds. Increase further for heavy scrapes (multi-section profiles, cold-start Chromium, slow networks/containers). |
| `LOGIN_TIMEOUT` | `1800` | Manual login wait timeout in seconds (`0` = no limit). Applies to the host-side `--login` browser; the container itself never opens one. |
| `LOGIN_INLINE_WAIT` | `25` | Bounded inline wait (seconds, max 45) for a tool call to resume after login completes. No effect in containers: the Docker runtime never opens a login window and raises a host-login-required error instead, so the session must be created on the host with `--login`. |
| `BROWSER_WAIT` | `25` | How long (seconds, max 45) to wait for another server process to hand over the shared browser before reporting that it is busy. `0` reports busy immediately. |
| `BROWSER_MIN_HOLD` | `20` | Shortest time (seconds) a process keeps the shared browser before handing it to a waiting process. Higher means fewer browser restarts but longer waits for other clients; clamped below `BROWSER_WAIT` so a waiting client is served before its own timeout. `0` hands over after every tool call. |
| `BROWSER_IDLE_TIMEOUT` | `600` | Close an idle browser and release the shared profile after this many seconds without a tool call. `0` keeps it open until the server exits. |
| `AUTO_IMPORT_FROM_BROWSER` | on by default | Auto-import a LinkedIn session from a locally logged-in browser on the first no-session tool call, before falling back to manual login. On by default across interactive and non-interactive desktop runs; set `false` to require `--login` / `--import-from-browser`. No effect in containers (no host browser or keychain) or on a non-loopback HTTP bind. On macOS the OS keychain may prompt once for Safe Storage access. |
| `USER_AGENT` | - | Custom browser user agent |
| `TRANSPORT` | `stdio` | Transport mode: stdio, streamable-http |
| `HOST` | `127.0.0.1` | HTTP server host (for streamable-http transport) |
| `PORT` | `8000` | HTTP server port (for streamable-http transport) |
| `HTTP_PATH` | `/mcp` | HTTP server path (for streamable-http transport) |
| `SLOW_MO` | `0` | Delay between browser actions in ms (debugging) |
| `VIEWPORT` | `1280x720` | Browser viewport size as WIDTHxHEIGHT |
| `CHROME_PATH` | - | Path to Chrome/Chromium executable (rarely needed in Docker) |
| `PROXY_SERVER` | - | Route the browser through a proxy, as `scheme://host:port` (`http`, `https`, `socks4`, `socks5`). May also carry credentials directly (`http://user:pass@host:port`), which is how most providers hand them out. Inside a container `127.0.0.1` is the container itself: for a relay running on the host use `host.docker.internal` (on native Linux Docker, add `--add-host=host.docker.internal:host-gateway`). Only browser traffic is routed, not the MCP transport. |
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
