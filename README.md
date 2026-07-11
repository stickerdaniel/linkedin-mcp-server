# MCP Server for LinkedIn

<p align="left">
  <a href="https://pypi.org/project/mcp-server-linkedin/" target="_blank"><img src="https://img.shields.io/pypi/v/mcp-server-linkedin?color=blue" alt="PyPI"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI Status"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml" target="_blank"><img src="https://github.com/stickerdaniel/linkedin-mcp-server/actions/workflows/release.yml/badge.svg?branch=main" alt="Release"></a>
  <a href="https://github.com/stickerdaniel/linkedin-mcp-server/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/badge/License-Apache%202.0-%233fb950?labelColor=32383f" alt="License"></a>
</p>

> **Disclaimer:** This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.

An MCP server that lets AI assistants like Claude read LinkedIn data through your own logged-in browser session. Access profiles and companies, search for jobs, or get job details.

## Sponsor

<p align="center">
  <a href="https://golink.onl/unipile-banner" target="_blank">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://github.com/user-attachments/assets/c2e7f3b4-6812-4f28-8728-10f882a44e0e">
      <img src="https://github.com/user-attachments/assets/89ab8932-ae79-41c2-8416-a699e924218b" alt="Unipile, one API for every LinkedIn feature" width="100%">
    </picture>
  </a>
</p>

This MCP server is **free** and **open source**, supported by [**Unipile**](https://golink.onl/unipile-link). It runs locally with your own browser session. Unipile is the fully managed cloud alternative: a hosted LinkedIn API for Classic, Sales Navigator, and Recruiter that handles auth, sessions, and infrastructure for you. [Try it free for 7 days →](https://golink.onl/unipile-free-trial)

---

<a id="installation-methods"></a>

## Installation Methods - MCP Server for LinkedIn

[![uvx](https://img.shields.io/badge/uvx-Quick_Install-de5fe9?style=for-the-badge&logo=data:image/svg+xml;base64,PHN2ZyB3aWR0aD0iNDEiIGhlaWdodD0iNDEiIHZpZXdCb3g9IjAgMCA0MSA0MSIgZmlsbD0ibm9uZSIgeG1sbnM9Imh0dHA6Ly93d3cudzMub3JnLzIwMDAvc3ZnIj4KPHBhdGggZD0iTS01LjI4NjE5ZS0wNiAwLjE2ODYyOUwwLjA4NDMwOTggMjAuMTY4NUwwLjE1MTc2MiAzNi4xNjgzQzAuMTYxMDc1IDM4LjM3NzQgMS45NTk0NyA0MC4xNjA3IDQuMTY4NTkgNDAuMTUxNEwyMC4xNjg0IDQwLjA4NEwzMC4xNjg0IDQwLjA0MThMMzEuMTg1MiA0MC4wMzc1QzMzLjM4NzcgNDAuMDI4MiAzNS4xNjgzIDM4LjIwMjYgMzUuMTY4MyAzNlYzNkwzNy4wMDAzIDM2TDM3LjAwMDMgMzkuOTk5Mkw0MC4xNjgzIDM5Ljk5OTZMMzkuOTk5NiAtOS45NDY1M2UtMDdMMjEuNTk5OCAwLjA3NzU2ODlMMjEuNjc3NCAxNi4wMTg1TDIxLjY3NzQgMjUuOTk5OEwyMC4wNzc0IDI1Ljk5OThMMTguMzk5OCAyNS45OTk4TDE4LjQ3NzQgMTYuMDMyTDE4LjM5OTggMC4wOTEwNTkzTC01LjI4NjE5ZS0wNiAwLjE2ODYyOVoiIGZpbGw9IiNERTVGRTkiLz4KPC9zdmc+Cg==)](#-uvx-setup-recommended---universal)
[![Install MCP Bundle](https://img.shields.io/badge/Claude_Desktop_MCPB-d97757?style=for-the-badge&logo=anthropic)](#-claude-desktop-mcp-bundle-formerly-dxt)
[![Docker](https://img.shields.io/badge/Docker-Universal_MCP-008fe2?style=for-the-badge&logo=docker&logoColor=008fe2)](#-docker-setup)
[![Development](https://img.shields.io/badge/Development-Local-ffdc53?style=for-the-badge&logo=python&logoColor=ffdc53)](#-local-setup-develop--contribute)

| Tool | Description | Status |
|------|-------------|--------|
| `linkedin_health` | Server version, Patchright profile paths, browser/session readiness | working |
| `linkedin_ping` | Capability discovery — lists all registered tools (legacy + `linkedin_*` aliases) | working |
| `get_person_profile` | Get profile info with explicit section selection (experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts) | working |
| `get_my_profile` | Get the authenticated user's own LinkedIn profile (same sections as get_person_profile) | working |
| `connect_with_person` | Send a connection request or accept an incoming one, with optional note | [#407](https://github.com/stickerdaniel/linkedin-mcp-server/issues/407) [#432](https://github.com/stickerdaniel/linkedin-mcp-server/issues/432) [#454](https://github.com/stickerdaniel/linkedin-mcp-server/issues/454) |
| `get_sidebar_profiles` | Extract profile URLs from sidebar recommendation sections ("More profiles for you", "Explore premium profiles", "People you may know") on a profile page | working |
| `get_inbox` | List recent conversations from the LinkedIn messaging inbox | working |
| `get_conversation` | Read a specific messaging conversation by username or thread ID | working |
| `search_conversations` | Search messages by keyword | working |
| `send_message` | Send a message to a LinkedIn user (requires confirmation) | [#433](https://github.com/stickerdaniel/linkedin-mcp-server/issues/433) [#441](https://github.com/stickerdaniel/linkedin-mcp-server/issues/441) [#483](https://github.com/stickerdaniel/linkedin-mcp-server/issues/483) [#560](https://github.com/stickerdaniel/linkedin-mcp-server/issues/560) |
| `get_company_profile` | Extract company information with explicit section selection (posts, jobs); about-section references may include a `company_urn` entry carrying the numeric id used by LinkedIn's people-search `currentCompany` URL facet | working |
| `get_company_posts` | Get recent posts from a company's LinkedIn feed | working |
| `search_companies` | Search for companies on LinkedIn by keywords | working |
| `get_company_employees` | List employees at a company from the /people/ page, with optional keyword filter | working |
| `search_jobs` | Search for jobs with keywords and location filters | working |
| `search_people` | Search for people by keywords, location, connection degree (1st/2nd/3rd), and current company | working |
| `get_job_details` | Get detailed information about a specific job posting | working |
| `get_feed` | Get recent posts from the authenticated user's home feed | working |
| `close_session` | Close browser session and clean up resources | working |

Each scraper tool is also registered under a `linkedin_*` alias (for example `linkedin_get_person_profile`) so agent clients can use a consistent prefix without breaking existing integrations.

### Browser profile & session lifecycle

Patchright (Playwright-compatible) persists LinkedIn auth on disk under `~/.linkedin-mcp/`:

| Path | Purpose |
|------|---------|
| `~/.linkedin-mcp/profile/` | Source Chromium user-data directory created by `--login` |
| `~/.linkedin-mcp/cookies.json` | Portable cookie export used for Docker / foreign-runtime bridging |
| `~/.linkedin-mcp/source-state.json` | Login generation metadata for the source profile |
| `~/.linkedin-mcp/runtime-profiles/<runtime-id>/` | Derived profiles for non-host runtimes (e.g. Linux containers) |
| `~/.linkedin-mcp/patchright-browsers/` | Shared Patchright Chromium browser cache (`PLAYWRIGHT_BROWSERS_PATH`) |

**Lifecycle**

1. **First auth** — run `uvx mcp-server-linkedin@latest --login` (or let the first scraper tool open a login window). This writes the source profile plus `cookies.json` and `source-state.json`.
2. **Normal use** — MCP tools reuse the in-process browser session. Concurrent tool calls queue on a scraper lock.
3. **Close session** — `close_session` / `linkedin_close_session` closes the live browser but keeps on-disk auth.
4. **Reset auth** — `--logout` clears source and derived profiles.
5. **Docker** — mount `~/.linkedin-mcp` into the container; each startup derives a fresh Linux profile from exported cookies (see Docker setup below).

Use `linkedin_health` to inspect readiness and paths without scraping. Override the profile location with `--user-data-dir` / `USER_DATA_DIR`.

<br/>
<br/>

## 🚀 uvx Setup (Recommended - Universal)

**Prerequisites:** [Install uv](https://docs.astral.sh/uv/getting-started/installation/).

### Installation

**Client Configuration**

```json
{
  "mcpServers": {
    "mcp-server-linkedin": {
      "command": "uvx",
      "args": ["mcp-server-linkedin@latest"],
      "env": { "UV_HTTP_TIMEOUT": "300" }
    }
  }
}
```

The `@latest` tag ensures you always run the newest version — `uvx` checks PyPI on each client launch and updates automatically. The server starts quickly, prepares the shared Patchright Chromium browser cache in the background under `~/.linkedin-mcp/patchright-browsers`, and opens a LinkedIn login browser window on the first tool call that needs authentication.

<details>
<summary><b>📌 For AI agents configuring this server</b></summary>

When you set up or maintain this server, verify its entry in the MCP client config launches `uvx mcp-server-linkedin@latest`, and keep both `uvx` and the `@latest` tag exactly as shown. Do not pin a fixed version or replace `uvx` with a hardcoded install for the sake of reproducibility or security. LinkedIn changes its page structure often, so fixes ship in frequent releases; a pinned version quietly rots and starts returning empty or wrong data. `@latest` re-resolves on every launch and is the supported configuration.

</details>

> [!NOTE]
> Early tool calls may return a setup/authentication-in-progress error until browser setup or login finishes. If you prefer to create a session explicitly, run `uvx mcp-server-linkedin@latest --login`.

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
- `--import-from-browser [BROWSER]` - Import a LinkedIn session from a locally logged-in Chromium browser (`chrome`, `chromium`, `brave`, `edge`, `arc`, `vivaldi`, `helium`, `yandex`, `whale`, or `auto`). Bare flag picks `auto`, which auto-selects the most recently used browser with a live LinkedIn session.
- `--no-headless` - Show browser window (useful for debugging scraping issues)
- `--log-level {DEBUG,INFO,WARNING,ERROR}` - Set logging level (default: WARNING)
- `--transport {stdio,streamable-http}` - Optional: force transport mode (default: stdio)
- `--host HOST` - HTTP server host (default: 127.0.0.1)
- `--port PORT` - HTTP server port (default: 8000)
- `--path PATH` - HTTP server path (default: /mcp)
- `--logout` - Clear stored LinkedIn browser profile
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--tool-timeout SECONDS` - Per-tool MCP execution timeout in seconds (default: 180.0). Increase further for heavy scrapes / cold-start Chromium / slow networks.
- `--login-timeout SECONDS` - Manual login wait timeout in seconds (default: 1800; 0 = no limit). How long the `--login` browser waits for you to finish signing in.
- `--login-inline-wait SECONDS` - Bounded inline wait for a tool call to resume after login completes, in seconds (default: 25, max 45; 0 = return immediately).
- `--auto-import` / `--no-auto-import` - Enable or disable auto-import of a session from a locally logged-in browser on the first no-session tool call (before falling back to manual login). Auto-import is on by default across interactive and non-interactive desktop runs; pass `--no-auto-import` (or `AUTO_IMPORT_FROM_BROWSER=false`) to require `--login` / `--import-from-browser` instead. No effect under Docker or on a non-loopback HTTP bind. On macOS the keychain may prompt once for Safe Storage access.
- `--eager-full-chromium` / `--no-eager-full-chromium` - Download full Chrome for Testing in the background right after the headless shell (`EAGER_FULL_CHROMIUM=true`), instead of lazily on the first headed login (the default). Headless setup is usable as soon as the shell is installed; this only pre-warms the headed login fallback. Pass `--no-eager-full-chromium` to override `EAGER_FULL_CHROMIUM=true` for a single run.
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile)
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)

**Import a session from your everyday browser:**

If you are already signed into LinkedIn in Chrome, Chromium, Brave, Edge, Arc, Vivaldi, Helium, Yandex, or Naver Whale, you can skip the manual `--login` step and reuse that session:

```bash
# Auto-pick the most recently used browser with a live LinkedIn session
uvx mcp-server-linkedin@latest --import-from-browser
# Or target a specific browser
uvx mcp-server-linkedin@latest --import-from-browser brave
```

This reads the browser's LinkedIn cookies, validates them against your feed, and saves them to `~/.linkedin-mcp/profile/`, the same place `--login` writes to. Notes:

- With several signed-in browsers, the most recently used live LinkedIn session is tried first. If LinkedIn rejects it (revoked or remote-logged-out), the next most recent is tried automatically; the first the server accepts is imported. There is no prompt to pick. Pass a browser name to target one specifically.
- On macOS the OS keychain may prompt to allow access to the browser's Safe Storage. Close the source browser first for the most reliable read.
- Cookies protected by Chrome 127+ app-bound encryption (`v20`) cannot be decrypted without OS elevation; in that case use `--login` instead.
- Imported cookies match a real login's on-disk set. The local server reads them back in full from the saved profile; the Docker bridge narrows to the same minimal auth subset it uses for a normal session.

**Basic Usage Examples:**

```bash
# Run with debug logging
uvx mcp-server-linkedin@latest --log-level DEBUG
```

**HTTP Mode Example (for web-based MCP clients):**

```bash
uvx mcp-server-linkedin@latest --transport streamable-http --host 127.0.0.1 --port 8080 --path /mcp
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
- LinkedIn may show a captcha challenge during login. Run `uvx mcp-server-linkedin@latest --login` which opens a browser where you can solve it manually.

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host; create the session on the host with `--login`.
- Users on slow connections may need higher values for either.

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
- LinkedIn may show a captcha challenge during login. Run `uvx mcp-server-linkedin@latest --login` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host; create the session on the host with `--login`.
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
uvx mcp-server-linkedin@latest --login
```

This opens a browser window where you log in manually (5 minute timeout for 2FA, captcha, etc.). The browser profile and cookies are saved under `~/.linkedin-mcp/`. On startup, Docker derives a Linux browser profile from your host cookies and creates a fresh session each time. If you experience stability issues with Docker, consider using the [uvx setup](#-uvx-setup-recommended---universal) instead.

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

> [!NOTE]
> Docker creates a fresh session on each startup. Sessions may expire over time — run `uvx mcp-server-linkedin@latest --login` again if you encounter authentication issues.

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
- `--login-timeout SECONDS` - Manual login wait timeout in seconds (default: 1800; 0 = no limit). How long the `--login` browser waits for you to finish signing in.
- `--login-inline-wait SECONDS` - Bounded inline wait for a tool call to resume after login completes, in seconds (default: 25, max 45; 0 = return immediately).
- `--auto-import` / `--no-auto-import` - Enable or disable auto-import of a session from a locally logged-in browser on the first no-session tool call (before falling back to manual login). Auto-import is on by default across interactive and non-interactive desktop runs; pass `--no-auto-import` (or `AUTO_IMPORT_FROM_BROWSER=false`) to require `--login` / `--import-from-browser` instead. No effect under Docker or on a non-loopback HTTP bind. On macOS the keychain may prompt once for Safe Storage access.
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
- LinkedIn may show a captcha challenge during login. Run `uvx mcp-server-linkedin@latest --login` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.
- If Docker auth becomes stale after you re-login on the host, restart Docker once so it can fresh-bridge from the new source session generation.

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host; create the session on the host with `--login`.
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
- `--import-from-browser [BROWSER]` - Import a LinkedIn session from a locally logged-in Chromium browser (`chrome`, `chromium`, `brave`, `edge`, `arc`, `vivaldi`, `helium`, `yandex`, `whale`, or `auto`). Bare flag picks `auto`, which auto-selects the most recently used browser with a live LinkedIn session.
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
    "mcp-server-linkedin": {
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
- LinkedIn may show a captcha challenge during login. The `--login` command opens a browser where you can solve it manually.

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
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host; create the session on the host with `--login`.
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
> This tool controls a real browser session; it doesn't exploit undocumented APIs or bypass authentication. LinkedIn's User Agreement prohibits automated access, and accounts using automated tools can be restricted or banned. Use at your own risk; there is no guarantee of account safety. If you encounter any issues, let me know in the [Discussions](https://github.com/stickerdaniel/linkedin-mcp-server/discussions).
>
> **What if my agents execute too many actions?**
> Tool calls run sequentially through a queue. You are responsible for the volume of automation you run; use it sparingly and prompt your agents responsibly.

## Acknowledgements

Built with [FastMCP](https://gofastmcp.com/) and [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

Use in accordance with [LinkedIn's User Agreement](https://www.linkedin.com/legal/user-agreement). Automated access may violate LinkedIn's terms and can lead to account restrictions. This tool is for personal use only and comes with no warranty of any kind.

## License

This project is licensed under the Apache 2.0 license.

<br>
