# LinkedIn MCP Server

A Model Context Protocol (MCP) server that connects AI assistants to LinkedIn. Access profiles, companies, and job postings through a Docker container.

## Features

- **Profile Access**: Get detailed LinkedIn profile information
- **Company Profiles**: Extract comprehensive company data
- **Job Details**: Retrieve job posting information
- **Job Search**: Search for jobs with keywords and location filters
- **People Search**: Search for people by keywords and location
- **Person Posts**: Get recent activity/posts from a person's profile
- **Company Posts**: Get recent posts from a company's LinkedIn feed
- **Compact References**: Return typed per-section links alongside readable text without shipping full-page markdown

## Quick Start

Create a browser profile locally, then mount it into Docker.

**Step 1: Create profile on the host (one-time setup)**

```bash
uvx linkedin-scraper-mcp --login
```

This creates the source session artifacts on the host:

- `~/.linkedin-mcp/profile/`
- `~/.linkedin-mcp/cookies.json`
- `~/.linkedin-mcp/source-state.json`

Docker foreign runtimes derive a Linux runtime profile under:

- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/profile/`
- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/storage-state.json`
- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/runtime-state.json`

By default, Docker now creates a fresh bridged Linux session on every startup using the minimal working auth cookie subset (`li_at`, `JSESSIONID`, `bcookie`, `bscookie`, `lidc`) and keeps that session alive for the server lifetime.

Runtime traces/logs are captured into an ephemeral run directory by default and are automatically preserved only when a scrape failure occurs. Set `LINKEDIN_TRACE_MODE=always` to keep every run or `LINKEDIN_TRACE_MODE=off` to disable trace persistence entirely.

If you want to experiment with persistent derived runtime reuse anyway, set `LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION=1`. In that mode, the first Docker run performs an internal checkpoint restart after `/feed/` succeeds and later Docker runs try to reuse the committed Linux runtime profile directly.

**Step 2: Configure Claude Desktop with Docker**

```json
{
  "mcpServers": {
    "linkedin": {
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

**Example with custom timeout:**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "~/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "-e", "TIMEOUT=10000",
        "stickerdaniel/linkedin-mcp-server"
      ]
    }
  }
}
```

## Repository

- **Source**: <https://github.com/stickerdaniel/linkedin-mcp-server>
- **License**: Apache 2.0
