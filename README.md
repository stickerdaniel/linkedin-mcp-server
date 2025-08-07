# LinkedIn MCP Server

<p align="left">
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-brightgreen?labelColor=32383f" alt="License"></a>
</p>

Through this LinkedIn MCP server, AI assistants like Claude can connect to your LinkedIn. Give access to profiles and companies, get your recommended jobs, or search for keywords. All from a Docker container on your machine.

## Installation Methods

[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup-recommended---universal)
[![Install DXT Extension](https://img.shields.io/badge/Claude_Desktop_DXT-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-dxt-extension)
[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-quick-install---universal)
[![Development](https://img.shields.io/badge/Development-Local-ffdc53?style=for-the-badge&logo=python&logoColor=ffdc53)](#-local-setup-develop--contribute)

https://github.com/user-attachments/assets/eb84419a-6eaf-47bd-ac52-37bc59c83680

## Usage Examples
```
What are my recommended jobs I can apply to?
```
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
> [!TIP]
> - **Profile Scraping** (`get_person_profile`): Get detailed information from a LinkedIn profile including work history, education, skills, and connections
> - **Company Analysis** (`get_company_profile`): Extract comprehensive company information from a LinkedIn company profile name
> - **Job Details** (`get_job_details`): Retrieve specific job posting details using LinkedIn job IDs
> - **Job Search** (`search_jobs`): Search for jobs with filters like keywords and location
> - **Recommended Jobs** (`get_recommended_jobs`): Get personalized job recommendations based on your profile
> - **Session Management** (`close_session`): Properly close browser session and clean up resources

> [!NOTE]
> July 2025: All tools are currently functional and actively maintained. If you encounter any issues, please report them in the [GitHub issues](https://github.com/stickerdaniel/linkedin-mcp-server/issues).

<br/>
<br/>

## 🐳 Docker Setup (Recommended - Universal)

**Prerequisites:** Make sure you have [Docker](https://www.docker.com/get-started/) installed and running.

### Installation

**Client Configuration:**
```json
{
  "mcpServers": {
    "linkedin": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "LINKEDIN_COOKIE",
        "stickerdaniel/linkedin-mcp-server:latest"
      ],
      "env": {
        "LINKEDIN_COOKIE": "li_at=YOUR_COOKIE_VALUE"
      }
    }
  }
}
```

### Getting the LinkedIn Cookie
<details>
<summary><b>🌐 Chrome DevTools Guide</b></summary>

1. Open LinkedIn and login
2. Open Chrome DevTools (F12 or right-click → Inspect)
3. Go to **Application** > **Storage** > **Cookies** > **https://www.linkedin.com**
4. Find the cookie named `li_at`
5. Copy the **Value** field (this is your LinkedIn session cookie)
6. Use this value as your `LINKEDIN_COOKIE` in the configuration

</details>
<details>
<summary><b>🐳 Docker get-cookie method</b></summary>

**Run the server with the `--get-cookie` flag:**
```bash
docker run -it --rm \
  stickerdaniel/linkedin-mcp-server:latest \
  --get-cookie
```
Copy the cookie from the output and set it as `LINKEDIN_COOKIE` in your client configuration. If this fails with a captcha challenge, use the method above.
</details>

> [!NOTE]
> The cookie will expire during the next 30 days. Just get the new cookie and update your client config. There are also many cookie manager extensions that you can use to quickly copy the cookie.

### Docker Setup Help
<details>
<summary><b>🔧 Configuration</b></summary>

**Transport Modes:**
- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For a web-based MCP server

**CLI Options:**
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--no-lazy-init` - Login to LinkedIn immediately instead of waiting for the first tool call
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--get-cookie` - Attempt to login with email and password and extract the LinkedIn cookie
- `--cookie {cookie}` - Pass a specific LinkedIn cookie for login
- `--user-agent {user_agent}` - Specify custom user agent string to prevent anti-scraping detection

**HTTP Mode Example (for web-based MCP clients):**
```bash
docker run -it --rm \
  -e LINKEDIN_COOKIE="li_at=YOUR_COOKIE_VALUE" \
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
- Ensure your LinkedIn cookie is set and correct
- Make sure you have only one active LinkedIn session per cookie at a time. Trying to open multiple sessions with the same cookie will result in a cookie invalid error.
- LinkedIn may require a login confirmation in the LinkedIn mobile app for --get-cookie
- You might get a captcha challenge if you logged in a lot of times in a short period of time, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode where you can debug the login process (solve captcha manually)
</details>

<br/>
<br/>

## 📦 Claude Desktop (DXT Extension)

**Prerequisites:** [Claude Desktop](https://claude.ai/download) and [Docker](https://www.docker.com/get-started/) installed

**One-click installation** for Claude Desktop users:
1. Download the [DXT extension](https://github.com/stickerdaniel/linkedin-mcp-server/releases/latest)
2. Double-click to install into Claude Desktop
3. Set your LinkedIn cookie in the extension settings

### Getting the LinkedIn Cookie
<details>
<summary><b>🌐 Chrome DevTools Guide</b></summary>

1. Open LinkedIn and login
2. Open Chrome DevTools (F12 or right-click → Inspect)
3. Go to **Application** > **Storage** > **Cookies** > **https://www.linkedin.com**
4. Find the cookie named `li_at`
5. Copy the **Value** field (this is your LinkedIn session cookie)
6. Use this value as your `LINKEDIN_COOKIE` in the configuration

</details>
<details>
<summary><b>🐳 Docker get-cookie method</b></summary>

**Run the server with the `--get-cookie` flag:**
```bash
docker run -it --rm \
  stickerdaniel/linkedin-mcp-server:latest \
  --get-cookie
```
Copy the cookie from the output and set it as `LINKEDIN_COOKIE` in your client configuration. If this fails with a captcha challenge, use the method above.
</details>

> [!NOTE]
> The cookie will expire during the next 30 days. Just get the new cookie and update your client config. There are also many cookie manager extensions that you can use to quickly copy the cookie.

### DXT Extension Setup Help
<details>
<summary><b>❗ Troubleshooting</b></summary>

**Docker issues:**
- Make sure [Docker](https://www.docker.com/get-started/) is installed
- Check if Docker is running: `docker ps`

**Login issues:**
- Ensure your LinkedIn cookie is set and correct
- Make sure you have only one active LinkedIn session per cookie at a time. Trying to open multiple sessions with the same cookie will result in a cookie invalid error.
- LinkedIn may require a login confirmation in the LinkedIn mobile app for --get-cookie
- You might get a captcha challenge if you logged in a lot of times in a short period of time, then try again later or follow the [local setup instructions](#-local-setup-develop--contribute) to run the server manually in --no-headless mode where you can debug the login process (solve captcha manually)
</details>

<br/>
<br/>

## 🚀 uvx Setup (Quick Install - Universal)

**Prerequisites:** Make sure you have [uv](https://docs.astral.sh/uv/) installed.

### Installation

Run directly from GitHub without cloning:

```bash
# Run directly from GitHub (latest version)
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --help

# Run with your LinkedIn cookie
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --cookie "li_at=YOUR_COOKIE_VALUE"
```

### Getting the LinkedIn Cookie
<details>
<summary><b>🌐 Chrome DevTools Guide</b></summary>

1. Open LinkedIn and login
2. Open Chrome DevTools (F12 or right-click → Inspect)
3. Go to **Application** > **Storage** > **Cookies** > **https://www.linkedin.com**
4. Find the cookie named `li_at`
5. Copy the **Value** field (this is your LinkedIn session cookie)
6. Use this value as your `LINKEDIN_COOKIE` in the configuration

</details>

<details>
<summary><b>🚀 uvx get-cookie method</b></summary>

**Run the server with the `--get-cookie` flag:**
```bash
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server \
  linkedin-mcp-server --get-cookie
```
Copy the cookie from the output and set it as `LINKEDIN_COOKIE` in your client configuration. If this fails with a captcha challenge, use the method above.
</details>

> [!NOTE]
> The cookie will expire during the next 30 days. Just get the new cookie and update your client config. There are also many cookie manager extensions that you can use to quickly copy the cookie.

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
      ],
      "env": {
        "LINKEDIN_COOKIE": "li_at=YOUR_COOKIE_VALUE"
      }
    }
  }
}
```

**Transport Modes:**
- **Default (stdio)**: Standard communication for local MCP servers
- **Streamable HTTP**: For web-based MCP server

**CLI Options:**
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--no-lazy-init` - Login to LinkedIn immediately instead of waiting for the first tool call
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--get-cookie` - Attempt to login with email and password and extract the LinkedIn cookie
- `--cookie {cookie}` - Pass a specific LinkedIn cookie for login
- `--user-agent {user_agent}` - Specify custom user agent string to prevent anti-scraping detection

**Basic Usage Examples:**
```bash
# Run with cookie from environment variable
LINKEDIN_COOKIE="YOUR_COOKIE_VALUE" uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server

# Run with cookie via flag
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --cookie "YOUR_COOKIE_VALUE"

# Run with debug logging
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --log-level DEBUG

# Extract cookie with credentials
uvx --from git+https://github.com/stickerdaniel/linkedin-mcp-server linkedin-mcp-server --get-cookie
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

**Cookie issues:**
- Ensure your LinkedIn cookie is set and correct
- Cookie can be passed via `--cookie` flag or `LINKEDIN_COOKIE` environment variable
- Make sure you have only one active LinkedIn session per cookie at a time

**Login issues:**
- LinkedIn may require a login confirmation in the LinkedIn mobile app for --get-cookie
- You might get a captcha challenge if you logged in a lot of times in a short period
</details>

<br/>
<br/>

## 🐍 Local Setup (Develop & Contribute)

**Prerequisites:** [Chrome browser](https://www.google.com/chrome/) and [Git](https://git-scm.com/downloads) installed

**ChromeDriver Setup:**
1. **Check Chrome version**: Chrome → menu (⋮) → Help → About Google Chrome
2. **Download matching ChromeDriver**: [Chrome for Testing](https://googlechromelabs.github.io/chrome-for-testing/)
3. **Make it accessible**:
   - Place ChromeDriver in PATH (`/usr/local/bin` on macOS/Linux)
   - Or set: `export CHROMEDRIVER_PATH=/path/to/chromedriver`
   - if no CHROMEDRIVER_PATH is set, the server will try to find it automatically by checking common locations

### Installation

```bash
# 1. Clone repository
git clone https://github.com/stickerdaniel/linkedin-mcp-server
cd linkedin-mcp-server

# 2. Install UV package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python # install python if you don't have it

# 3. Install dependencies and dev dependencies
uv sync
uv sync --group dev

# 4. Install pre-commit hooks
uv run pre-commit install

# 5. Start the server once manually
# You will be prompted to enter your LinkedIn credentials, and they will be securely stored in your OS keychain
# Once logged in, your cookie will be stored in your OS keychain and used for subsequent runs until it expires
uv run -m linkedin_mcp_server --no-headless --no-lazy-init
```

### Local Setup Help
<details>
<summary><b>🔧 Configuration</b></summary>

**CLI Options:**
- `--no-headless` - Show browser window (debugging)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--no-lazy-init` - Login to LinkedIn immediately instead of waiting for the first tool call
- `--get-cookie` - Login with email and password and extract the LinkedIn cookie
- `--clear-keychain` - Clear all stored LinkedIn credentials and cookies from system keychain
- `--cookie {cookie}` - Pass a specific LinkedIn cookie for login
- `--user-agent {user_agent}` - Specify custom user agent string to prevent anti-scraping detection
- `--transport {stdio,streamable-http}` - Set transport mode
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--help` - Show help

**HTTP Mode Example (for web-based MCP clients):**
```bash
uv run -m linkedin_mcp_server --transport streamable-http --host 127.0.0.1 --port 8000 --path /mcp
```

**Claude Desktop:**
```**json**
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
- Add `--no-lazy-init` to attempt to login to LinkedIn immediately instead of waiting for the first tool call
- Add `--log-level DEBUG` to see more detailed logging
- Make sure you have only one active LinkedIn session per cookie at a time. Trying to open multiple sessions with the same cookie will result in a cookie invalid error. E.g. if you have a logged in browser session with a docker container, you can't use the same cookie to login with the local setup while the docker container is running / session is not closed.

**ChromeDriver issues:**
- Ensure Chrome and ChromeDriver versions match
- Check ChromeDriver is in PATH or set `CHROMEDRIVER_PATH` in your env

**Python issues:**
- Check Python version: `uv python --version` (should be 3.12+)
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
