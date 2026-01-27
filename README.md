# LinkedIn MCP Server

<p align="left">
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-brightgreen?labelColor=32383f" alt="License"></a>
</p>

Through this LinkedIn MCP server, AI assistants like Claude can connect to your LinkedIn. Access profiles and companies, search for jobs, or get job details.

## Installation Methods

[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-recommended---universal)
[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup)
[![Install DXT Extension](https://img.shields.io/badge/Claude_Desktop_DXT-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-dxt-extension)
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

```
What has Anthropic been posting about recently? https://www.linkedin.com/company/anthropic/
```

## Features & Tool Status

| Tool | Description | Status |
|------|-------------|--------|
| `get_person_profile` | Get detailed profile info including work history, education, contacts, interests | Working |
| `get_company_profile` | Extract company information including employees, affiliated companies | Working |
| `get_company_posts` | Get recent posts from a company's LinkedIn feed | Working |
| `search_jobs` | Search for jobs with keywords and location filters | Working |
| `get_job_details` | Get detailed information about a specific job posting | Working |
| `close_session` | Close browser session and clean up resources | Working |

> [!WARNING]
> The browser profile at `~/.linkedin-mcp/browser-profile/` contains sensitive authentication data. Keep it secure and do not share it.

<br/>
<br/>

## 🚀 uvx Setup (Recommended - Universal)

**Prerequisites:** Make sure you have [uv](https://docs.astral.sh/uv/) and Playwright `uvx playwright install chromium` installed.

### Installation

**Step 1: Create a session (first time only)**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session
```

This opens a browser for you to log in manually (5 minute timeout for 2FA, captcha, etc.). The session is saved to `~/.linkedin-mcp/browser-profile/`.

**Step 2: Run the server**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server
```

> [!NOTE]
> Sessions may expire over time. If you encounter authentication issues, run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` again.

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

- `--get-session [PATH]` - Open browser to log in and save session (default: ~/.linkedin-mcp/browser-profile)
- `--no-headless` - Show browser window (useful for debugging scraping issues)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--clear-session` - Clear stored LinkedIn session file
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)

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

- Session is stored in `~/.linkedin-mcp/browser-profile/`
- Make sure you have only one active LinkedIn session at a time

**Login issues:**

- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in frequently. Run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` which opens a browser where you can solve it manually.

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

1. Open LinkedIn in your browser in an **incognito** tab and log in
2. Open DevTools (F12) → Application → Cookies → linkedin.com
3. Copy the `li_at` cookie value

#### Option 2: Session File (More Reliable)

Create a session file locally, then mount it into Docker.

**Step 1: Create session using uvx (one-time setup)**

```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The session is saved to `~/.linkedin-mcp/browser-profile/`.

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

> [!NOTE]
> **Why can't I run `--get-session` in Docker?** Docker containers don't have a display server. You have two options:
> 1. Create a session on your host using the [uvx setup](#-uvx-setup-recommended---universal) and mount it into Docker
> 2. Pass your `li_at` cookie via `LINKEDIN_COOKIE` (if you encounter auth challenges, use option 1 instead)

### Docker Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**

- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For a web-based MCP server

**CLI Options:**

- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--clear-session` - Clear stored LinkedIn session file
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (rarely needed in Docker)

> [!NOTE]
> `--get-session` and `--no-headless` are not available in Docker (no display server). Use the [uvx setup](#-uvx-setup-recommended---universal) to create sessions.

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
- You might get a captcha challenge if you logged in frequently. Run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.

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

## 📦 Claude Desktop (DXT Extension)

**Prerequisites:** [Claude Desktop](https://claude.ai/download) and [Docker](https://www.docker.com/get-started/) installed & running

**One-click installation** for Claude Desktop users:

1. Download the [DXT extension](https://github.com/stickerdaniel/linkedin-mcp-server/releases/latest)
2. Double-click to install into Claude Desktop
3. Create a session: `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session`

> [!NOTE]
> Sessions may expire over time. If you encounter authentication issues, run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` again.

### DXT Extension Setup Help

<details>
<summary><b>❗ Troubleshooting</b></summary>

**First-time setup timeout:**

- Claude Desktop has a ~60 second connection timeout
- If the Docker image isn't cached, the pull may exceed this timeout
- **Fix:** Pre-pull the image before first use:
  ```bash
  docker pull stickerdaniel/linkedin-mcp-server:2.3.0
  ```
- Then restart Claude Desktop

**Docker issues:**

- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in frequently. Run `uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-session` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

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

# 6. Create a session (first time only)
uv run -m linkedin_mcp_server --get-session

# 7. Start the server
uv run -m linkedin_mcp_server
```

### Local Setup Help

<details>
<summary><b>🔧 Configuration</b></summary>

**CLI Options:**

- `--get-session [PATH]` - Open browser to log in and save session (default: ~/.linkedin-mcp/browser-profile)
- `--no-headless` - Show browser window (useful for debugging scraping issues)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--clear-session` - Clear stored LinkedIn session file
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--session-info` - Check if current session is valid and exit
- `--linkedin-cookie COOKIE` - LinkedIn session cookie (li_at) for authentication
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

</details>

<details>
<summary><b>❗ Troubleshooting</b></summary>

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--get-session`
- You might get a captcha challenge if you logged in frequently. The `--get-session` command opens a browser where you can solve it manually.

**Scraping issues:**

- Use `--no-headless` to see browser actions and debug scraping problems
- Add `--log-level DEBUG` to see more detailed logging

**Session issues:**

- Session is stored in `~/.linkedin-mcp/browser-profile/`
- Use `--clear-session` to clear the session and start fresh

**Python/Playwright issues:**

- Check Python version: `python --version` (should be 3.12+)
- Reinstall Playwright: `uv run playwright install chromium`
- Reinstall dependencies: `uv sync --reinstall`

**Timeout issues:**

- If pages fail to load or elements aren't found, try increasing the timeout: `--timeout 10000`
- Users on slow connections may need higher values (e.g., 15000-30000ms)
- Can also set via environment variable: `TIMEOUT=10000`

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`

</details>

Feel free to open an [issue](https://github.com/stickerdaniel/linkedin-mcp-server/issues) or [PR](https://github.com/stickerdaniel/linkedin-mcp-server/pulls)!

<br/>
<br/>

## Acknowledgements

Built with [LinkedIn Scraper](https://github.com/joeyism/linkedin_scraper) by [@joeyism](https://github.com/joeyism) and [FastMCP](https://gofastmcp.com/).

⚠️ Use in accordance with [LinkedIn's Terms of Service](https://www.linkedin.com/legal/user-agreement). Web scraping may violate LinkedIn's terms. This tool is for personal use only.

## License

This project is licensed under the Apache 2.0 license.

<br>
