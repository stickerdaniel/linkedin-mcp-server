# LinkedIn MCP Server

<p align="left">
  <a href="https://pypi.org/project/linkedin-scraper-mcp/" target="_blank"><img src="https://img.shields.io/pypi/v/linkedin-scraper-mcp?color=blue" alt="PyPI"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-%233fb950?labelColor=32383f" alt="License"></a>
</p>

Connect ChatGPT, Claude, and other MCP clients to a real logged-in LinkedIn
browser session. The server can research profiles and companies, search people
and jobs, read messages and feed posts, and prepare draft-first outreach while
keeping write actions behind explicit approval.

## What You Can Do

- **Research people and companies** with profile sections, company pages,
  employee lists, and sidebar recommendations.
- **Search LinkedIn** for people, companies, jobs, and conversations.
- **Inspect jobs and feeds** with job details, company posts, home feed posts,
  and reusable LinkedIn references.
- **Prepare sales outreach** with lead briefs, personalized drafts, follow-up
  plans, and message-risk review. These tools never send on their own.
- **Use one server across clients**: Claude Desktop over `stdio`, Claude/ChatGPT
  over streamable HTTP, and ChatGPT data-only flows through `search`/`fetch`.

> [!IMPORTANT]
> This project controls a browser logged in as you. Use it for personal,
> low-volume workflows, respect LinkedIn's terms, and keep remote deployments
> behind HTTPS and authentication.

## Choose Your Setup

[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-recommended---universal)
[![Install MCP Bundle](https://img.shields.io/badge/Claude_Desktop_MCPB-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-mcp-bundle-formerly-dxt)
[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup)
[![Development](https://img.shields.io/badge/Development-Local-ffdc53?style=for-the-badge&logo=python&logoColor=ffdc53)](#-local-setup-develop--contribute)

| Use case | Best path | Why |
|----------|-----------|-----|
| Claude Desktop, easiest install | [MCP Bundle](#-claude-desktop-mcp-bundle-formerly-dxt) | One-click install and managed browser setup |
| Claude Desktop or local MCP clients | [uvx](#-uvx-setup-recommended---universal) | No clone required, updates from PyPI |
| ChatGPT/Claude remote MCP | [streamable HTTP](#-chatgpt-and-claude-remote-mcp) | Works with HTTPS plus `MCP_AUTH_TOKEN` |
| Container/server runtime | [Docker](#-docker-setup) | Reproducible headless runtime |
| Contributing | [Local setup](#-local-setup-develop--contribute) | Editable source, tests, linting |

## Tools At A Glance

| Group | Tools |
|-------|-------|
| People | `get_person_profile`, `get_my_profile`, `search_people`, `get_sidebar_profiles` |
| Companies | `get_company_profile`, `get_company_posts`, `search_companies`, `get_company_employees` |
| Jobs and feed | `search_jobs`, `get_job_details`, `get_feed` |
| Messaging | `get_inbox`, `get_conversation`, `search_conversations`, `send_message` |
| ChatGPT compatibility | `search`, `fetch` |
| Outreach | `research_lead`, `draft_outreach_message`, `plan_follow_up`, `review_outreach_target` |
| Session/actions | `connect_with_person`, `close_session` |

> [!NOTE]
> `send_message` and `connect_with_person` are write actions and require explicit
> confirmation. Outreach tools are read-only/draft-only.

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
- `--tool-timeout SECONDS` - Per-tool MCP execution timeout in seconds (default: 180.0). Increase further for heavy scrapes / cold-start Chromium / slow networks.
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

For private remote MCP testing with ChatGPT or Claude over HTTP, set a bearer
token and expose the server only through HTTPS:

```bash
MCP_AUTH_TOKEN="change-me" \
uvx linkedin-scraper-mcp@latest \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8080 \
  --path /mcp
```

Clients must send:

```text
Authorization: Bearer change-me
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

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- Users on slow connections may need higher values for either.

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>

<br/>
<br/>

## 🤖 ChatGPT and Claude Remote MCP

This server exposes one unified MCP surface:

- Claude Desktop and local clients can keep using the existing `stdio` setup.
- Claude remote MCP, ChatGPT Developer Mode, and other HTTP MCP clients can use
  `streamable-http` with `MCP_AUTH_TOKEN`.
- ChatGPT data-only/deep-research style clients can use the generic read-only
  `search` and `fetch` tools. `search` returns compact ids such as
  `person:alice`, `company:acme`, and `job:12345`; `fetch` also accepts post
  ids such as `post:/feed/update/...` when a post permalink is already known.

For real remote use, put the HTTP endpoint behind HTTPS and do not bind directly
to a public interface without authentication. The bearer token is intended for
private deployments; OAuth remains the better fit for a public multi-user app.

### Sales Outreach Workflow

The outreach tools are draft-first. They help ChatGPT or Claude research a lead,
draft a personalized note, plan follow-ups, and review risk, but they do not send
messages. Actual sends still go through `send_message` or `connect_with_person`,
which remain separate destructive tools requiring explicit approval.

Typical flow:

1. Use `search`/`search_people` or `search_companies` to find a target.
2. Use `fetch` or `research_lead` to collect profile and company context.
3. Use `draft_outreach_message` to create a connection note or DM.
4. Use `review_outreach_target` to check specificity and spam-risk signals.
5. Human approves before calling `send_message` or `connect_with_person`.

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

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- Users on slow connections may need higher values for either.

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
- `--tool-timeout SECONDS` - Per-tool MCP execution timeout in seconds (default: 180.0). Increase further for heavy scrapes / cold-start Chromium / slow networks.
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

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- Users on slow connections may need higher values for either.

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
- `--tool-timeout SECONDS` - Per-tool MCP execution timeout in seconds (default: 180.0). Increase further for heavy scrapes / cold-start Chromium / slow networks.
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

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- Users on slow connections may need higher values for either.

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>


<br/>
<br/>

> [!IMPORTANT]
> **FAQ**
>
> **Is this safe to use? Will I get banned?**
> This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. That said, LinkedIn's TOS prohibit automated tools. With normal usage (not bulk scraping!) you're not risking a ban. So far, no users have been banned for using this MCP. If you encounter any issues, let me know in the [Discussions](https://github.com/stickerdaniel/linkedin-mcp-server/discussions).
>
> **What if my agents execute too many actions?**
> LinkedIn may send you a warning about automated tool usage. If that happens, reduce your automation volume. This MCP executes tool calls sequentially via a queue but has no built-in rate limits. Prompt your agents responsibly.

## Acknowledgements

Built with [FastMCP](https://gofastmcp.com/) and [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

Use in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.

## Give a Gift

I don't accept donations — I'm not in need of them — but if you'd like to send a small gift to show appreciation, you can use any of the addresses below. Your support is appreciated, but entirely optional.

| Type | Address |
|------|---------|
| ETH on Ethereum | `0xb20b50f362d9F35CE1a311c0b4B15C2551A09567` |
| SOL on Solana | `27rwcHUjQKNTMVnvsu1GhQ1RiVnLVy4WyFF8xJoz84DQ` |
| BTC on Bitcoin | `bc1qrmppveguzaw6gqw6qphnc02sje6z80ql5rcdmr` |

## License

This project is licensed under the Apache 2.0 license.

<br>
