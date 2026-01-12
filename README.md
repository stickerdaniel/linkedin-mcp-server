# LinkedIn MCP Server

<p align="left">
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-brightgreen?labelColor=32383f" alt="License"></a>
</p>

Through this LinkedIn MCP server, AI assistants like Claude can connect to your LinkedIn. Give access to profiles and companies, search for jobs, or get job details. All from a Docker container on your machine.

## Installation Methods

[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup-recommended---universal)
[![Install DXT Extension](https://img.shields.io/badge/Claude_Desktop_DXT-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-dxt-extension)
[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-quick-install---universal)
[![Development](https://img.shields.io/badge/Development-Local-ffdc53?style=for-the-badge&logo=python&logoColor=ffdc53)](#-local-setup-develop--contribute)

<https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680>

## Usage Examples

```
Research the background of this candidate https://www.linkedin.com/in/stickerdaniel/
```

```
Get this company profile for partnership discussions https://www.linkedin.com/company/inframs/
```

```
Suggest improvements for my CV to target this job posting https://www.linkedin.com/jobs/view/4252026496
```

## Features & Tool Status
>
> [!TIP]
>
> - **Profile Scraping** (`get_person_profile`): Get detailed information from a LinkedIn profile including work history, education, skills, and connections
> - **Company Analysis** (`get_company_profile`): Extract comprehensive company information from a LinkedIn company profile name
> - **Job Search** (`search_jobs`): Search for jobs with keywords and location filters
> - **Job Details** (`get_job_details`): Get detailed information about a specific job posting
> - **Session Management** (`close_session`): Properly close browser session and clean up resources

**Tool Status:**

| Tool | Status |
|------|--------|
| `get_person_profile` | Working |
| `get_company_profile` | Working |
| `search_jobs` | Broken (upstream) |
| `get_job_details` | Working |
| `close_session` | Working |

<br/>
<br/>

## 🚀 uvx Setup (Recommended - Universal)

**Prerequisites:** Make sure you have [uv](https://docs.astral.sh/uv/) installed.

### Installation

**Step 1: Create a session (first time only)**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server \
  linkedin-mcp-server --get-session
```

This opens a browser for you to log in manually (5 minute timeout for 2FA, captcha, etc.). The session is saved to `~/.linkedin-mcp/session.json`.

**Step 2: Run the server**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server
```

> [!NOTE]
> Sessions may expire over time. If you encounter authentication issues, run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` again. For debugging login issues, use `--no-headless` to see the browser window.

### uvx Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**Client Configuration:**

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/stickerdaniel/linkedin-mcp-server",
        "linkedin-mcp-server"
      ]
    }
  }
}
```

**Transport Modes:**

- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For web-based MCP server

**CLI Options:**

- `--no-headless` - Show browser window (useful for login and debugging)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--get-session [PATH]` - Login interactively and save session (default: ~/.linkedin-mcp/session.json)
- `--clear-session` - Clear stored LinkedIn session file

**Basic Usage Examples:**

```bash
# Create a session interactively
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session

# Run with debug logging
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --log-level DEBUG
```

**HTTP Mode Example (for web-based MCP clients):**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server \
  --transport streamable-http --host 127.0.0.1 --port 8080 --path /mcp
```

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

**Session issues:**

- Session is stored at `~/.linkedin-mcp/session.json`
- Make sure you have only one active LinkedIn session at a time

**Login issues:**

- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in frequently

</details>

<br/>
<br/>

## 🐳 Docker Setup

**Prerequisites:** Make sure you have [Docker](https://www.docker.com/get-started/) installed and running.

### Authentication Options

Docker runs headless (no browser window), so you need to authenticate using one of these methods:

#### Option 1: Cookie Authentication (Simplest)

Get your LinkedIn `li_at` cookie and pass it to Docker:

```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": ["run", "-i", "--rm", "-e", "LINKEDIN_COOKIE", "stickerdaniel/linkedin-mcp-server"],
      "env": {
        "LINKEDIN_COOKIE": "your_li_at_cookie_value"
      }
    }
  }
}
```

**To get your `li_at` cookie:**

1. Open LinkedIn in your browser and log in
2. Open DevTools (F12) → Application → Cookies → linkedin.com
3. Copy the `li_at` cookie value

#### Option 2: Session File (More Reliable)

Create a session file locally, then mount it into Docker.

**Step 1: Create session using uvx (one-time setup)**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The session is saved to `~/.linkedin-mcp/session.json`.

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
> Sessions may expire over time. If you encounter authentication issues, run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` again locally, or use a fresh `li_at` cookie.

> [!WARNING]
> The session file at `~/.linkedin-mcp/session.json` contains sensitive authentication data. Keep it secure and do not share it.

> [!NOTE]
> **Why can't I run `--get-session` in Docker?** Docker containers don't have a display server, so Playwright can't show a browser window. You must create the session on your host machine first, then mount it into Docker.

### Docker Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**

- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For a web-based MCP server

**CLI Options:**

- `--no-headless` - Show browser window (useful for login and debugging)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--get-session [PATH]` - Login interactively and save session (default: ~/.linkedin-mcp/session.json)
- `--clear-session` - Clear stored LinkedIn session file

**HTTP Mode Example (for web-based MCP clients):**

```bash
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 8080:8080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --transport streamable-http --host 0.0.0.0 --port 8080 --path /mcp
```

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
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in a lot of times in a short period of time, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode where you can debug the login process (solve captcha manually)

</details>

<br/>
<br/>

## 📦 Claude Desktop (DXT Extension)

**Prerequisites:** [Claude Desktop](https://claude.ai/download) and [Docker](https://www.docker.com/get-started/) installed

**One-click installation** for Claude Desktop users:

1. Download the [DXT extension](https://github.com/stickerdaniel/linkedin-mcp-server/releases/latest)
2. Double-click to install into Claude Desktop
3. Create a session using `--get-session` (see Docker instructions above)

> [!NOTE]
> Sessions may expire over time. If you encounter authentication issues, run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` again. For debugging login issues, use the [local setup](#-local-setup-develop--contribute) with `--no-headless` mode.

### DXT Extension Setup Help

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Docker issues:**

- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in frequently, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode

</details>

<br/>
<br/>

## 🐍 Local Setup (Develop & Contribute)

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

# 4. Install Playwright browser
uv run playwright install chromium

# 5. Install pre-commit hooks
uv run pre-commit install

# 6. Start the server (first run opens browser for manual login)
# Login in the browser window - session will be saved to ~/.linkedin-mcp/session.json
uv run -m linkedin_mcp_server --no-headless
```

### Local Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**CLI Options:**

- `--no-headless` - Show browser window (useful for login and debugging)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--get-session [PATH]` - Login interactively and save session (default: ~/.linkedin-mcp/session.json)
- `--clear-session` - Clear stored LinkedIn session file
- `--help` - Show help

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

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Login/Scraping issues:**

- Use `--no-headless` to see browser actions (captcha challenge, LinkedIn mobile app 2fa, ...)
- Add `--log-level DEBUG` to see more detailed logging
- Make sure you have only one active LinkedIn session at a time

**Session issues:**

- Session is stored in `~/.linkedin-mcp/session.json`
- Use `--clear-session` to clear the session and start fresh

**Python/Playwright issues:**

- Check Python version: `python --version` (should be 3.12+)
- Reinstall Playwright: `uv run playwright install chromium`
- Reinstall dependencies: `uv sync --reinstall`

</details>

Feel free to open an [issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) or [PR](https://github.com/stickerdaniel/linkedin-mcp-server/pulls)!

<br/>
<br/>

## Acknowledgements

Built with [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) by [@joeyism](https://github.com/joeyism) and [FastMCP](https://gofastmcp.com/).

⚠️ Use in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.

## Star History

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=stickerdaniel/linkedin-mcp-server&type=Date&theme=dark" />
  <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=stickerdaniel/linkedin-mcp-server&type=Date" />
  <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=stickerdaniel/linkedin-mcp-server&type=Date" />
</picture>

## License

This project is licensed under the Apache 2.0 license.

<br>
