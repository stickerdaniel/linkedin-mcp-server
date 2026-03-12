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
# Installed package usage
uvx linkedin-scraper-mcp --login

# Local development from this repo
uv run -m linkedin_mcp_server --login
```

If you are debugging or verifying code changes in this repository, prefer `uv run -m linkedin_mcp_server ...` so the running process matches your workspace files. Use `uvx` when intentionally testing the packaged distribution.

This creates the source session artifacts on the host:

- `~/.linkedin-mcp/profile/`
- `~/.linkedin-mcp/cookies.json`
- `~/.linkedin-mcp/source-state.json`

The first Docker run derives a persistent Linux runtime profile under:

- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/profile/`
- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/storage-state.json`
- `~/.linkedin-mcp/runtime-profiles/linux-amd64-container/runtime-state.json`

That first Docker run also performs an internal checkpoint restart after `/feed/` succeeds, so the derived Linux runtime session is committed immediately instead of depending on later browser shutdown. Later Docker runs reuse that committed Linux runtime profile directly. Re-running `--login` on the host creates a new source login generation, and the next Docker run rebuilds its derived Linux profile once.

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
