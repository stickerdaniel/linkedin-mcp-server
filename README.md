# LinkedIn MCP Server

<p align="left">
  <a href="https://pypi.org/project/linkedin-scraper-mcp/" target="_blank"><img src="https://img.shields.io/pypi/v/linkedin-scraper-mcp?color=blue" alt="PyPI"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-%233fb950?labelColor=32383f" alt="License"></a>
</p>

Through this LinkedIn MCP server, AI assistants like Claude can connect to your LinkedIn. Access profiles and companies, search for jobs, or get job details.


> [!IMPORTANT]
> **FAQ**
>
> **Is this safe to use? Will I get banned?**
> This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. That said, LinkedIn's TOS prohibit automated tools. With normal usage (not bulk scraping!) you're not risking a ban. So far, no users have been banned for using this MCP. If you encounter any issues, let me know in the [Discussions](https://github.com/stickerdaniel/linkedin-mcp-server/discussions).
>
> **What if my agents execute too many actions?**
> LinkedIn may send you a warning about automated tool usage. If that happens, reduce your automation volume. This MCP executes tool calls sequentially via a queue but has no built-in rate limits. Prompt your agents responsibly.

## Installation Methods

[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-recommended---universal)
[![Install MCP Bundle](https://img.shields.io/badge/Claude_Desktop_MCPB-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-mcp-bundle-formerly-dxt)
[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup)
[![Development](https://img.shields.io/badge/Development-Local-ffdc53?style=for-the-badge&logo=python&logoColor=ffdc53)](#-local-setup-develop--contribute)

<https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680>

## Features & Tool Status

| Tool | Description | Status |
|------|-------------|--------|
| `get_person_profile` | Get profile info with explicit section selection (experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts) | working |
| `connect_with_person` | Send a connection request or accept an incoming one, with optional note | [#304](https://github.com/stickerdaniel/linkedin-mcp-server/issues/304) [#365](https://github.com/stickerdaniel/linkedin-mcp-server/issues/365) |
| `get_sidebar_profiles` | Extract profile URLs from sidebar recommendation sections ("More profiles for you", "Explore premium profiles", "People you may know") on a profile page | working |
| `get_inbox` | List recent conversations from the LinkedIn messaging inbox | working |
| `get_conversation` | Read a specific messaging conversation by username or thread ID | [#307](https://github.com/stickerdaniel/linkedin-mcp-server/issues/307) |
| `search_conversations` | Search messages by keyword | working |
| `send_message` | Send a message to a LinkedIn user (requires confirmation) | working |
| `get_company_profile` | Extract company information with explicit section selection (posts, jobs) | working |
| `get_company_posts` | Get recent posts from a company's LinkedIn feed | working |
| `search_jobs` | Search for jobs with keywords and location filters | working |
| `search_people` | Search for people by keywords and location | working |
| `get_job_details` | Get detailed information about a specific job posting | working |
| `get_saved_jobs` | Get saved/bookmarked jobs from your LinkedIn job tracker | [#330](https://github.com/stickerdaniel/linkedin-mcp-server/issues/330) |
| `close_session` | Close browser session and clean up resources | working |

<br/>
<br/>

## 🚀 uvx Setup (Recommended - Universal)

**Prerequisites:** [Install uv](https://docs.astral.sh/uv/getting-started/installation/).

### Installation

**Client Configuration**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": ["linkedin-scraper-mcp@latest"],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

The `@latest` tag ensures you always run the newest version — `uvx` checks PyPI on each client launch and updates automatically. The server starts quickly, prepares the shared Patchright Chromium browser cache in the background under `~/.linkedin-mcp/patchright-browsers`, and opens a LinkedIn login browser window on the first tool call that needs authentication.

> [!NOTE]
> Early tool calls may return a setup/authentication-in-progress error until browser setup or login finishes. If you prefer to create a session explicitly, run `uvx linkedin-scraper-mcp@latest --login`.

### uvx Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**

- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For web-based MCP server
- If no transport is specified, the server defaults to `stdio`
- An interactive terminal without explicit transport shows a chooser prompt

**CLI Options:**

- `--login` - Open browser to log in and save persistent profile
- `--no-headless` - Show browser window (useful for debugging scraping issues)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Optional: force transport mode (default: stdio)
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--logout` - Clear stored LinkedIn browser profile
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)

**Basic Usage Examples:**

```bash
# Run with debug logging
uvx linkedin-scraper-mcp@latest --log-level DEBUG
```

**HTTP Mode Example (for web-based MCP clients):**

```bash
uvx linkedin-scraper-mcp@latest --transport streamable-http --host 127.0.0.1 --port 8080 --path /mcp
```

Runtime server logs are emitted by FastMCP/Uvicorn.

Tool calls are serialized within a single server process to protect the shared
LinkedIn browser session. Concurrent client requests queue instead of running in
parallel. Use `--log-level DEBUG` to see scraper lock wait/acquire/release logs.

**Test with mcp inspector:**

1. Install and run mcp inspector ```bunx @modelcontextprotocol/inspector```
2. Click pre-filled token url to open the inspector in your browser
3. Select `Streamable HTTP` as `Transport Type`
4. Set `URL` to `http://localhost:8080/mcp`
5. Connect
6. Test tools

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Installation issues:**

- Ensure you have uv installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Check uv version: `uv --version` (should be 0.4.0 or higher)
- On first run, `uvx` downloads all Python dependencies. On slow connections, uv's default 30s HTTP timeout may be too short. The recommended config above already sets `UV_HTTP_TIMEOUT=300` (seconds) to avoid this.

**Session issues:**

- Browser profile is stored at `~/.linkedin-mcp/profile/`
- Managed browser downloads are cached at `~/.linkedin-mcp/patchright-browsers/`
- Make sure you have only one active LinkedIn session at a time

**Login issues:**

- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- You might get a captcha challenge if you logged in frequently. Run `uvx linkedin-scraper-mcp@latest --login` which opens a browser where you can solve it manually.

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>

<br/>
<br/>

## 📦 Claude Desktop MCP Bundle (formerly DXT)

**Prerequisites:** [Claude Desktop](https://claude.ai/download).

**One-click installation** for Claude Desktop users:

1. Download the latest `.mcpb` artifact from [releases](https://github.com/stickerdaniel/linkedin-mcp-server/releases/latest)
2. Click the downloaded `.mcpb` file to install it into Claude Desktop
3. Call any LinkedIn tool

On startup, the MCP Bundle starts preparing the shared Patchright Chromium browser cache in the background. If you call a tool too early, Claude will surface a setup-in-progress error. On the first tool call that needs authentication, the server opens a LinkedIn login browser window and asks you to retry after sign-in.

### MCP Bundle Setup Help

<details>
<summary><b>❗ Troubleshooting</b></summary>

**First-time setup behavior:**

- Claude Desktop starts the bundle immediately; browser setup continues in the background
- If the Patchright Chromium browser is still downloading, retry the tool after a short wait
- Managed browser downloads are shared under `~/.linkedin-mcp/patchright-browsers/`

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- You might get a captcha challenge if you logged in frequently. Run `uvx linkedin-scraper-mcp@latest --login` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

</details>

<br/>
<br/>

## 🐳 Docker Setup

**Prerequisites:** Make sure you have [Docker](https://www.docker.com/get-started/) installed and running, and [uv](https://docs.astral.sh/uv/getting-started/installation/) installed on the host for the one-time `--login` step.

### Authentication

Docker runs headless (no browser window), so you need to create a browser profile locally first and mount it into the container.

**Step 1: Create profile on the host (one-time setup)**

```bash
uvx linkedin-scraper-mcp@latest --login
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The browser profile and cookies are saved under `~/.linkedin-mcp/`. On startup, Docker derives a Linux browser profile from your host cookies and creates a fresh session each time. If you experience stability issues with Docker, consider using the [uvx setup](#-uvx-setup-recommended---universal) instead.

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

> [!NOTE]
> Docker creates a fresh session on each startup. Sessions may expire over time — run `uvx linkedin-scraper-mcp@latest --login` again if you encounter authentication issues.

> [!NOTE]
> **Why can't I run `--login` in Docker?** Docker containers don't have a display server. Create a profile on your host using the [uvx setup](#-uvx-setup-recommended---universal) and mount it into Docker.

### Docker Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**

- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For a web-based MCP server
- If no transport is specified, the server defaults to `stdio`
- An interactive terminal without explicit transport shows a chooser prompt

**CLI Options:**

- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Optional: force transport mode (default: stdio)
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--logout` - Clear all stored LinkedIn auth state, including source and derived runtime profiles
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (rarely needed in Docker)

> [!NOTE]
> `--login` and `--no-headless` are not available in Docker (no display server). Use the [uvx setup](#-uvx-setup-recommended---universal) to create profiles.

**HTTP Mode Example (for web-based MCP clients):**

```bash
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 8080:8080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --transport streamable-http --host 0.0.0.0 --port 8080 --path /mcp
```

Runtime server logs are emitted by FastMCP/Uvicorn.

**Test with mcp inspector:**

1. Install and run mcp inspector ```bunx @modelcontextprotocol/inspector```
2. Click pre-filled token url to open the inspector in your browser
3. Select `Streamable HTTP` as `Transport Type`
4. Set `URL` to `http://localhost:8080/mcp`
5. Connect
6. Test tools

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Docker issues:**

- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- You might get a captcha challenge if you logged in frequently. Run `uvx linkedin-scraper-mcp@latest --login` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.
- If Docker auth becomes stale after you re-login on the host, restart Docker once so it can fresh-bridge from the new source session generation.

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>

<br/>
<br/>

## 🐍 Local Setup (Develop & Contribute)

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) for architecture guidelines and checklists. Please [open an issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) first to discuss the feature or bug fix before submitting a PR.

**Prerequisites:** [Git](https://git-scm.com/downloads) and [uv](https://docs.astral.sh/uv/) installed

### Installation

```bash
# 1. Clone repository
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server

# 2. Install UV package manager (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 3. Install dependencies
uv sync
uv sync --group dev

# 4. Install pre-commit hooks
uv run pre-commit install

# 5. Start the server
uv run -m linkedin_mcp_server
```

The local server uses the same managed-runtime flow as MCPB and `uvx`: it prepares the Patchright Chromium browser cache in the background and opens LinkedIn login on the first auth-requiring tool call. You can still run `uv run -m linkedin_mcp_server --login` when you want to create the session explicitly.

### Local Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**CLI Options:**

- `--login` - Open browser to log in and save persistent profile
- `--no-headless` - Show browser window (useful for debugging scraping issues)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Optional: force transport mode (default: stdio)
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--logout` - Clear stored LinkedIn browser profile
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--status` - Check if current session is valid and exit
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)
- `--slow-mo MS` - Delay between browser actions in milliseconds (default: 0, useful for debugging)
- `--user-agent STRING` - Custom browser user agent
- `--viewport WxH` - Browser viewport size (default: 1280x720)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)
- `--help` - Show help

> **Note:** Most CLI options have environment variable equivalents. See `.env.example` for details.

**HTTP Mode Example (for web-based MCP clients):**

```bash
uv run -m linkedin_mcp_server --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

**Claude Desktop:**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uv",
      "args": ["--directory", "/path/to/linkedin-mcp-server", "run", "-m", "linkedin_mcp_server"]
    }
  }
}
```

`stdio` is used by default for this config.

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- You might get a captcha challenge if you logged in frequently. The `--login` command opens a browser where you can solve it manually.

**Scraping issues:**

- Use `--no-headless` to see browser actions and debug scraping problems
- Add `--log-level DEBUG` to see more detailed logging

**Session issues:**

- Browser profile is stored at `~/.linkedin-mcp/profile/`
- Use `--logout` to clear the profile and start fresh

**Python/Patchright issues:**

- Check Python version: `python --version` (should be 3.12+)
- Reinstall Patchright: `uv run patchright install chromium`
- Reinstall dependencies: `uv sync --reinstall`

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>


<br/>
<br/>

## Acknowledgements

Built with [FastMCP](https://gofastmcp.com/) and [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

Use in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.

## License

This project is licensed under the Apache 2.0 license.

<br>
