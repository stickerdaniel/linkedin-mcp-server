# LinkedIn MCP Server

A Model Context Protocol (MCP) server that connects AI assistants to LinkedIn. Access profiles, companies, and job postings through a Docker container.

## Features

- **Profile Access**: Get detailed LinkedIn profile information including experience, education, skills, projects, certifications, and more
- **Own Profile**: Fetch the authenticated user's own profile to give agents self-context
- **Profile Connections**: Send connection requests or accept incoming ones, with optional notes
- **Company Profiles**: Extract comprehensive company data
- **Company Employees**: List employees at a company with optional keyword filtering
- **Company Search**: Search for companies by keyword
- **Job Details**: Retrieve job posting information
- **Job Search**: Search for jobs with keywords and location filters
- **People Search**: Search for people by keywords and location
- **Person Posts**: Get recent activity/posts from a person's profile
- **Company Posts**: Get recent posts from a company's LinkedIn feed
- **Compact References**: Return typed per-section links alongside readable text without shipping full-page markdown

## Quick Start

Create a browser profile locally, then mount it into Docker. You still need [uv](https://docs.astral.sh/uv/getting-started/installation/) installed on the host for the one-time `uvx linkedin-scraper-mcp@latest --login` step. Docker already includes its own Chromium runtime, so the managed Patchright Chromium browser download used by MCPB/`uvx` is not needed here.

**Step 1: Create profile on the host (one-time setup)**

```bash
uvx linkedin-scraper-mcp@latest --login
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The browser profile and cookies are saved under `~/.linkedin-mcp/`. On startup, Docker derives a Linux browser profile from your host cookies and creates a fresh session each time. For better stability, consider the [uvx setup](https://github.com/stickerdaniel/linkedin-mcp-server#-uvx-setup-recommended---universal).

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
