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
- **People Search**: Search for people by keywords and location
- **Person Posts**: Get recent activity/posts from a person's profile
- **Company Posts**: Get recent posts from a company's LinkedIn feed
- **Home Feed**: Get recent posts from the authenticated user's LinkedIn home feed
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
> **Note:** Tool calls are serialized within one server process to protect the
> shared LinkedIn browser session. Concurrent client requests queue instead of
> running in parallel. Use `LOG_LEVEL=DEBUG` to see scraper lock logs.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_DATA_DIR` | `~/.linkedin-mcp/profile` | Path to persistent browser profile directory |
| `LOG_LEVEL` | `WARNING` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TIMEOUT` | `5000` | Browser timeout in milliseconds |
| `TOOL_TIMEOUT` | `180` | Per-tool MCP execution timeout in seconds. Increase further for heavy scrapes (multi-section profiles, cold-start Chromium, slow networks/containers). |
| `LOGIN_TIMEOUT` | `1800` | Manual login wait timeout in seconds (`0` = no limit). Applies to the host-side `--login` browser; the container itself never opens one. |
| `LOGIN_INLINE_WAIT` | `25` | Bounded inline wait (seconds, max 45) for a tool call to resume after login completes. No effect in containers: the Docker runtime never opens a login window and raises a host-login-required error instead, so the session must be created on the host with `--login`. |
| `AUTO_IMPORT_FROM_BROWSER` | on by default | Auto-import a LinkedIn session from a locally logged-in browser on the first no-session tool call, before falling back to manual login. On by default across interactive and non-interactive desktop runs; set `false` to require `--login` / `--import-from-browser`. No effect in containers (no host browser or keychain) or on a non-loopback HTTP bind. On macOS the OS keychain may prompt once for Safe Storage access. |
| `USER_AGENT` | - | Custom browser user agent |
| `TRANSPORT` | `stdio` | Transport mode: stdio, streamable-http |
| `HOST` | `127.0.0.1` | HTTP server host (for streamable-http transport) |
| `PORT` | `8000` | HTTP server port (for streamable-http transport) |
| `HTTP_PATH` | `/mcp` | HTTP server path (for streamable-http transport) |
| `SLOW_MO` | `0` | Delay between browser actions in ms (debugging) |
| `VIEWPORT` | `1280x720` | Browser viewport size as WIDTHxHEIGHT |
| `CHROME_PATH` | - | Path to Chrome/Chromium executable (rarely needed in Docker) |
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
