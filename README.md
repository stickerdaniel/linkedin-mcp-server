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
| `get_person_profile` | Get profile info with explicit section selection (experience, education, interests, honors, languages, certifications, skills, projects, contact_info, posts) | [#590](https://github.com/stickerdaniel/linkedin-mcp-server/issues/590) |
| `get_my_profile` | Get the authenticated user's own LinkedIn profile (same sections as get_person_profile) | [#590](https://github.com/stickerdaniel/linkedin-mcp-server/issues/590) |
| `connect_with_person` | Send a connection request or accept an incoming one, with optional note | [#407](https://github.com/stickerdaniel/linkedin-mcp-server/issues/407) [#432](https://github.com/stickerdaniel/linkedin-mcp-server/issues/432) [#454](https://github.com/stickerdaniel/linkedin-mcp-server/issues/454) [#629](https://github.com/stickerdaniel/linkedin-mcp-server/issues/629) |
| `get_sidebar_profiles` | Extract profile URLs from sidebar recommendation sections ("More profiles for you", "Explore premium profiles", "People you may know") on a profile page | working |
| `get_inbox` | List recent conversations from the LinkedIn messaging inbox | working |
| `get_conversation` | Read a specific messaging conversation by username or thread ID | working |
| `search_conversations` | Search messages by keyword | working |
| `send_message` | Send a message to a LinkedIn user (requires confirmation) | [#433](https://github.com/stickerdaniel/linkedin-mcp-server/issues/433) [#441](https://github.com/stickerdaniel/linkedin-mcp-server/issues/441) [#483](https://github.com/stickerdaniel/linkedin-mcp-server/issues/483) [#560](https://github.com/stickerdaniel/linkedin-mcp-server/issues/560) [#573](https://github.com/stickerdaniel/linkedin-mcp-server/issues/573) |
| `get_company_profile` | Extract company information with explicit section selection (posts, jobs); about-section references may include a `company_urn` entry carrying the numeric id used by LinkedIn's people-search `currentCompany` URL facet | working |
| `get_company_posts` | Get recent posts from a company's LinkedIn feed | working |
| `search_companies` | Search for companies on LinkedIn by keywords | working |
| `get_company_employees` | List employees at a company from the /people/ page, with optional keyword filter | working |
| `search_jobs` | Search for jobs with keywords and location filters | working |
| `get_saved_jobs` | List job postings saved by the authenticated user | working |
| `search_people` | Search for people by keywords, location, connection degree (1st/2nd/3rd), and current company | [#526](https://github.com/stickerdaniel/linkedin-mcp-server/issues/526) |
| `get_job_details` | Get detailed information about a specific job posting | working |
| `get_feed` | Get recent posts from the authenticated user's home feed | working |
| `search_posts` | Search posts/content globally by keyword (the "Posts" tab) with an optional recency filter (past-24h/past-week/past-month) | working |
| `schedule_post` | Schedule a feed post via LinkedIn's native Schedule-for-later flow (requires confirmation; publishes even when this server is offline) | working |
| `get_scheduled_posts` | List the authenticated user's scheduled posts (scheduled moment + body preview per entry) | working |
| `edit_scheduled_post` | Edit a scheduled post's body and/or scheduled moment in place, selected by a body-text snippet (requires confirmation) | working |
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
- `--login-timeout SECONDS` - Manual login wait timeout in seconds (default: 1800; 0 = no limit). How long the `--login` browser waits for you to finish signing in. `--login-viewer` caps the effective limit at its 1,800-second viewer wall, so `0` becomes 30 minutes in viewer mode; protected profile restoration may finish afterward.
- `--login-viewer` - With `--login` inside Docker, expose the login browser through one token-authenticated noVNC URL on port 6080. Requires a writable, non-memory mount covering the authentication root above the configured profile.
- `--login-inline-wait SECONDS` - Bounded inline wait for a tool call to resume after login completes, in seconds (default: 25, max 45; 0 = return immediately).
- `--browser-wait SECONDS` - How long to wait for another server process to hand over the shared browser (default: 25, max 45; 0 = report busy at once). Only matters when several MCP clients run at the same time.
- `--browser-min-hold SECONDS` - Shortest time this process keeps the shared browser before handing it to a waiting process (default: 20, clamped below `--browser-wait` so a waiting client is served before its own timeout; 0 = hand over after every tool call). Raising it means fewer browser restarts but longer waits for other clients.
- `--browser-idle-timeout SECONDS` - Close an idle browser and release the shared profile after this long without a tool call (default: 600; 0 = keep it open until the server exits).
- `--auto-import` / `--no-auto-import` - Enable or disable auto-import of a session from a locally logged-in browser on the first no-session tool call (before falling back to manual login). Auto-import is on by default across interactive and non-interactive desktop runs; pass `--no-auto-import` (or `AUTO_IMPORT_FROM_BROWSER=false`) to require `--login` / `--import-from-browser` instead. No effect under Docker or on a non-loopback HTTP bind. On macOS the keychain may prompt once for Safe Storage access.
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile). Rotating or clearing a session moves and deletes this directory *and its parent*, which also holds `cookies.json`, `source-state.json` and the derived runtime profiles. A path other than the default is therefore only used once it carries a `profile-claim.json` marker, written automatically when the parent is empty or already holds a session from this server
- `--claim-profile-root` - Take over a non-default profile directory this server will not claim on its own: one whose parent already holds other files, or one carrying an ownership marker written for a different path (a mounted volume that moved). Needed once
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)
- `--proxy-server URL` - Route the browser through a proxy, as `scheme://host:port`. Set the password via `PROXY_PASSWORD` (no flag, so it stays out of the process list)

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

Tool calls are serialized to protect the shared LinkedIn browser session, both
within one server process and across separate ones. If you run several MCP
clients at once, each starts its own server process, and only one of them uses
the browser at a time; the others wait briefly and take over as soon as it
finishes a call. A client that waits too long gets a "browser is busy" message
and can simply retry. Use `--log-level DEBUG` to see the wait/acquire/release
logs.

This covers processes on the same machine and in the same runtime. It does not
extend between the host and a Docker container sharing the same
`~/.linkedin-mcp` directory, so do not run `--login` or `--logout` on the host
while a container is running.

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
- *Windows, `DLL load failed while importing _greenlet`*: move to greenlet 3.5.5 or newer, whose published Windows wheels carry the C++ runtime inside the extension again. A fresh `uvx` run resolves that on its own; an environment that pins its dependencies needs `uv lock --upgrade-package greenlet`. Only greenlet 3.3.1 through 3.5.4 need `MSVCP140.dll`, which neither the python.org installer nor the `uv`-managed builds carry, and a greenlet built from source can need it at any version. Where the version cannot be moved, the [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) supplies that DLL. Reported as [greenlet#525](https://github.com/python-greenlet/greenlet/issues/525), fixed in [greenlet#526](https://github.com/python-greenlet/greenlet/pull/526).

**Session issues:**

- Browser profile is stored at `~/.linkedin-mcp/profile/`
- Managed browser downloads are cached at `~/.linkedin-mcp/patchright-browsers/`
- *The browser cache keeps growing*: a server upgrade can bring a new Chromium revision, and Patchright keeps the old one for as long as any installed version still references it. `uvx` keeps one archive per version you have ever run, so every one of them holds such a reference and the old revisions stay. The server logs a warning naming the revisions it is holding and how much space they take. To reclaim it, stop every LinkedIn MCP Server instance, delete `~/.linkedin-mcp/patchright-browsers/`, and let the next launch download the current browser.
- Make sure you have only one active LinkedIn session at a time

**Login issues:**

- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- LinkedIn may show a captcha challenge during login. Run `uvx mcp-server-linkedin@latest --login` which opens a browser where you can solve it manually.

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host. Create the session on the host with `--login`, or use the explicit Docker `--login --login-viewer` command.
- Users on slow connections may need higher values for either.

**Told to run `--login` on the host when you already did:**

- If tool calls answer "No valid LinkedIn session is available in Docker" on a machine that is *not* a container, the runtime was misdetected. This happened on Linux hosts running a Docker daemon for unrelated services. Set `LINKEDIN_MCP_CONTAINER=false` to override the detection; `true` forces the opposite.

**Using a proxy:**

> **Most people should not use one.** LinkedIn's own guidance for reducing
> security challenges is to avoid a VPN or proxy, and it scores the addresses a
> session signs in from. A home connection you have used for years is a trust
> signal; a commercial exit node with a history you cannot see is not, and
> switching to one is itself the kind of change that triggers a checkpoint.
> A proxy is worth it in one case: the server runs somewhere its address is
> obviously a data centre, or in a different country from the account's history.
> Even then, a WireGuard or Tailscale exit node on your own home network beats
> any paid provider, because the address really is yours. If you do buy one,
> take a dedicated static ISP address and keep it, rather than a rotating
> residential pool.

- Route the browser through a proxy with `--proxy-server http://host:port` (`http`, `https`, `socks4` and `socks5` are accepted). Only browser traffic is routed, not the MCP transport.
- Credentials go in `PROXY_USERNAME` and `PROXY_PASSWORD`. There is no `--proxy-password` flag on purpose: command-line arguments are readable by every other user on the machine. `PROXY_SERVER` also accepts the combined `http://user:pass@host:port` form most providers hand out.
- Chromium cannot authenticate to a SOCKS proxy, so credentials require an `http(s)` endpoint. If your provider only offers authenticated SOCKS5, run a local relay that holds the credentials and point the server at that.
- Local addresses go through the proxy too. Chromium's usual direct route for `localhost` is removed when a proxy is set, so add `PROXY_BYPASS=localhost,127.0.0.1,::1` if you need local targets reached directly.
- Auto-import is skipped while a proxy is configured: a session taken from a local browser was created on your real address, and moving it to the proxy is the very change that triggers a checkpoint. Use `--login`.
- A wrong proxy password does not report itself: Chromium retries the authentication challenge until the page times out, so it surfaces as a timeout or a failed sign-in. If sessions stop working right after you add a proxy, check the credentials before assuming the session expired.
- **Set the proxy up before creating the session.** Run `--login` with the proxy already configured. Turning a proxy on for an existing profile moves a logged-in session to a new IP, which is what triggers a LinkedIn checkpoint. The same applies to `--import-from-browser`, which imports a session created on your real IP. Use a sticky session, not a rotating pool, for the same reason.

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`
- On macOS and Linux the browser must be at least as new as the one that last opened your profile, and the server refuses the launch otherwise. (Not on Windows: a browser there cannot be asked its version without starting one, so the check is off.) An older browser can silently drop stores a newer one wrote, the saved session among them, and the failure then looks exactly like an expired login. The message names both versions. Going back to the bundled Chromium after running a newer Chrome once is the usual way to meet this; either run the newer browser again, whichever one that was, or run `--login`, which moves the stored session aside and signs in fresh with the browser you have. `--logout` also clears it but discards the old session instead of keeping it recoverable, and it asks for confirmation on the terminal, so it is not usable from a server an MCP client started.
- Only Chrome, Chromium and Chrome for Testing are compared this way. Forks number themselves differently (Vivaldi is on 7.x, Edge's build number sits far below Chrome's under the same major), so pointing `CHROME_PATH` at one turns the check off rather than producing a refusal nothing could satisfy.

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
- *The browser cache keeps growing*: Patchright keeps an old Chromium revision for as long as any installed version still references it, so an upgrade can leave both on disk. The server logs a warning naming what it holds. To reclaim the space, stop every LinkedIn MCP Server instance, delete `~/.linkedin-mcp/patchright-browsers/`, and let the next launch download the current browser.
- *Windows, the bundle exits with `DLL load failed while importing _greenlet`*: install the [Microsoft Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist), or reinstall a bundle pinning greenlet 3.5.5 or newer, whose published Windows wheels carry the C++ runtime inside the extension again. A bundle pinning greenlet 3.3.1 through 3.5.4 needs `MSVCP140.dll` from that redistributable, which neither the python.org installer nor the `uv`-managed builds carry, and a greenlet built from source can need it at any version. The server names this itself on startup, and only after checking that the loader cannot produce that DLL. Reported as [greenlet#525](https://github.com/python-greenlet/greenlet/issues/525), fixed in [greenlet#526](https://github.com/python-greenlet/greenlet/pull/526).

**Login issues:**

- Make sure you have only one active LinkedIn session at a time
- LinkedIn may require a login confirmation in the LinkedIn mobile app for `--login`
- LinkedIn may show a captcha challenge during login. Run `uvx mcp-server-linkedin@latest --login` which opens a browser where you can solve captchas manually. See the [uvx setup](#-uvx-setup-recommended---universal) for prerequisites.

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host. Create the session on the host with `--login`, or use the explicit Docker `--login --login-viewer` command.
- Users on slow connections may need higher values for either.

**Told to run `--login` on the host when you already did:**

- If tool calls answer "No valid LinkedIn session is available in Docker" on a machine that is *not* a container, the runtime was misdetected. This happened on Linux hosts running a Docker daemon for unrelated services. Set `LINKEDIN_MCP_CONTAINER=false` to override the detection; `true` forces the opposite.

</details>

<br/>
<br/>

## 🐳 Docker Setup

**Prerequisites:** Make sure [Docker](https://www.docker.com/get-started/) is installed and running.

### Authentication

Docker includes an authenticated, short-lived browser viewer for explicit login. Create the host directory before mounting it so the unprivileged container user can write the session:

```bash
mkdir -p ~/.linkedin-mcp
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 127.0.0.1:6080:6080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --login --login-viewer
```

Open the complete loopback URL printed by the command. Its token stays in the URL fragment, so the initial HTTP request does not carry it. Static viewer files are public on port 6080; the token protects the WebSocket that controls the browser. The viewer fixes client-side scaling, keeps remote resize disabled, and closes after login, failure, a stop signal, or 1,800 seconds. Protected profile restoration may finish after remote control has closed, because interrupting a move on the mounted auth root could split the previous session. The profile mount is required before any existing session can be rotated. For the default profile, use the exact `-v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp` mapping above. If an older rootful Docker run created that host directory as root, repair it with `sudo chown -R "$(id -u):$(id -g)" ~/.linkedin-mcp`.

A profile created by the Docker viewer belongs to the container runtime and is reused directly on later Docker startups with the same runtime identity. A profile created on the host with `uvx mcp-server-linkedin@latest --login` or `--import-from-browser` belongs to a foreign runtime, so Docker derives a fresh Linux bridge from its source cookies on each startup.

**Configure Claude Desktop with Docker**

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
> Docker reuses a source profile created by the viewer under the same runtime identity. Host-created and otherwise foreign source profiles use a fresh Linux bridge on each startup. Sessions may expire over time; repeat the Docker viewer command above or run `uvx mcp-server-linkedin@latest --login` on the host.

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
- `--logout` - Clear all stored LinkedIn auth state, including source and derived runtime profiles and any retired sessions
- `--timeout MS` - Browser timeout for page operations in milliseconds (default: 5000)
- `--tool-timeout SECONDS` - Per-tool MCP execution timeout in seconds (default: 180.0). Increase further for heavy scrapes / cold-start Chromium / slow networks.
- `--login-timeout SECONDS` - Manual login wait timeout in seconds (default: 1800; 0 = no limit). How long the `--login` browser waits for you to finish signing in. `--login-viewer` caps the effective limit at its 1,800-second viewer wall, so `0` becomes 30 minutes in viewer mode; protected profile restoration may finish afterward.
- `--login-viewer` - With `--login` inside Docker, expose the login browser through one token-authenticated noVNC URL on port 6080. Requires a writable, non-memory mount covering the authentication root above the configured profile.
- `--login-inline-wait SECONDS` - Bounded inline wait for a tool call to resume after login completes, in seconds (default: 25, max 45; 0 = return immediately).
- `--browser-wait SECONDS` - How long to wait for another server process to hand over the shared browser (default: 25, max 45; 0 = report busy at once). Only matters when several MCP clients run at the same time.
- `--browser-min-hold SECONDS` - Shortest time this process keeps the shared browser before handing it to a waiting process (default: 20, clamped below `--browser-wait` so a waiting client is served before its own timeout; 0 = hand over after every tool call). Raising it means fewer browser restarts but longer waits for other clients.
- `--browser-idle-timeout SECONDS` - Close an idle browser and release the shared profile after this long without a tool call (default: 600; 0 = keep it open until the server exits).
- `--auto-import` / `--no-auto-import` - Enable or disable auto-import of a session from a locally logged-in browser on the first no-session tool call (before falling back to manual login). Auto-import is on by default across interactive and non-interactive desktop runs; pass `--no-auto-import` (or `AUTO_IMPORT_FROM_BROWSER=false`) to require `--login` / `--import-from-browser` instead. No effect under Docker or on a non-loopback HTTP bind. On macOS the keychain may prompt once for Safe Storage access.
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile). Rotating or clearing a session moves and deletes this directory *and its parent*, which also holds `cookies.json`, `source-state.json` and the derived runtime profiles. A path other than the default is therefore only used once it carries a `profile-claim.json` marker, written automatically when the parent is empty or already holds a session from this server
- `--claim-profile-root` - Take over a non-default profile directory this server will not claim on its own: one whose parent already holds other files, or one carrying an ownership marker written for a different path (a mounted volume that moved). Needed once
- `--chrome-path PATH` - Path to Chrome/Chromium executable (rarely needed in Docker)
- `--proxy-server URL` - Route the browser through a proxy, as `scheme://host:port`. Set the password via `PROXY_PASSWORD` (no flag, so it stays out of the process list)

> [!NOTE]
> Plain `--login` still has no visible window in Docker. Add `--login-viewer` and publish `127.0.0.1:6080:6080` only for the one-shot login command. Docker is already headed by default, so `--no-headless` changes nothing. The experimental `--daemon` is ignored in Docker because its owner can outlive the virtual display.

**HTTP Mode Example (for web-based MCP clients):**

```bash
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 127.0.0.1:8080:8080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --transport streamable-http --host 0.0.0.0 --port 8080 --path /mcp
```

Both halves of that are needed, and they do different jobs. `--host 0.0.0.0`
makes the server reachable *inside* the container: a process bound to
`127.0.0.1` in there cannot be reached through a published port at all. The
`127.0.0.1:` in front of `-p` is what limits it *outside*, to this machine.
Drop that prefix and Docker publishes on every interface, which puts an
endpoint with no authentication on your network. The server cannot tell the two
apart, so it warns either way.

Loopback publishing limits this to the machine, not to the container. Other
containers on the same host can still reach it through `host.docker.internal`
wherever that name resolves, which is the default on Docker Desktop and
OrbStack but not on native Linux Docker.

Runtime server logs are emitted by FastMCP/Uvicorn.

The HTTP server answers requests addressed to `localhost` or to the address it
is bound to, and refuses others with `421`. That is what stops a website you
merely visit from pointing a domain at this server and using your LinkedIn
session through your own browser.

Reaching the server by any other name is refused, including a machine name on
your network and the public name in front of a reverse proxy. Either have the
proxy rewrite the upstream `Host` to the backend address, or name the host you
serve it under:

```bash
FASTMCP_HTTP_ALLOWED_HOSTS='["mcp.example"]'
```

That permits exactly that name and keeps refusing everything else. The endpoint
still has no authentication, so anything reachable beyond your own machine
belongs behind something that provides it.

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
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host. Create the session on the host with `--login`, or use the explicit Docker `--login --login-viewer` command.
- Users on slow connections may need higher values for either.

**Told to run `--login` on the host when you already did:**

- If tool calls answer "No valid LinkedIn session is available in Docker" on a machine that is *not* a container, the runtime was misdetected. This happened on Linux hosts running a Docker daemon for unrelated services. Set `LINKEDIN_MCP_CONTAINER=false` to override the detection; `true` forces the opposite.

**Using a proxy:**

> **Most people should not use one.** LinkedIn's own guidance for reducing
> security challenges is to avoid a VPN or proxy, and it scores the addresses a
> session signs in from. A home connection you have used for years is a trust
> signal; a commercial exit node with a history you cannot see is not, and
> switching to one is itself the kind of change that triggers a checkpoint.
> A proxy is worth it in one case: the server runs somewhere its address is
> obviously a data centre, or in a different country from the account's history.
> Even then, a WireGuard or Tailscale exit node on your own home network beats
> any paid provider, because the address really is yours. If you do buy one,
> take a dedicated static ISP address and keep it, rather than a rotating
> residential pool.

- Route the browser through a proxy with `--proxy-server http://host:port` (`http`, `https`, `socks4` and `socks5` are accepted). Only browser traffic is routed, not the MCP transport.
- Credentials go in `PROXY_USERNAME` and `PROXY_PASSWORD`. There is no `--proxy-password` flag on purpose: command-line arguments are readable by every other user on the machine. `PROXY_SERVER` also accepts the combined `http://user:pass@host:port` form most providers hand out.
- Chromium cannot authenticate to a SOCKS proxy, so credentials require an `http(s)` endpoint. If your provider only offers authenticated SOCKS5, run a local relay that holds the credentials and point the server at that.
- Local addresses go through the proxy too. Chromium's usual direct route for `localhost` is removed when a proxy is set, so add `PROXY_BYPASS=localhost,127.0.0.1,::1` if you need local targets reached directly.
- Auto-import is skipped while a proxy is configured: a session taken from a local browser was created on your real address, and moving it to the proxy is the very change that triggers a checkpoint. Use `--login`.
- A wrong proxy password does not report itself: Chromium retries the authentication challenge until the page times out, so it surfaces as a timeout or a failed sign-in. If sessions stop working right after you add a proxy, check the credentials before assuming the session expired.
- **Set the proxy up before creating the session.** Run `--login` with the proxy already configured. Turning a proxy on for an existing profile moves a logged-in session to a new IP, which is what triggers a LinkedIn checkpoint. The same applies to `--import-from-browser`, which imports a session created on your real IP. Use a sticky session, not a rotating pool, for the same reason.

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`
- On macOS and Linux the browser must be at least as new as the one that last opened your profile, and the server refuses the launch otherwise. (Not on Windows: a browser there cannot be asked its version without starting one, so the check is off.) An older browser can silently drop stores a newer one wrote, the saved session among them, and the failure then looks exactly like an expired login. The message names both versions. Going back to the bundled Chromium after running a newer Chrome once is the usual way to meet this; either run the newer browser again, whichever one that was, or run `--login`, which moves the stored session aside and signs in fresh with the browser you have. `--logout` also clears it but discards the old session instead of keeping it recoverable, and it asks for confirmation on the terminal, so it is not usable from a server an MCP client started.
- Only Chrome, Chromium and Chrome for Testing are compared this way. Forks number themselves differently (Vivaldi is on 7.x, Edge's build number sits far below Chrome's under the same major), so pointing `CHROME_PATH` at one turns the check off rather than producing a refusal nothing could satisfy.
- In the documented Docker setup this check does not apply. The container never opens the profile you created with `--login`; it derives its own from your cookies, and by default rebuilds that from scratch on every start, so there is nothing for an older image to downgrade. With `EXPERIMENTAL_PERSIST_DERIVED_RUNTIME` the derived profile is kept, and an image tag that moves backwards then throws it away and re-derives it, again with nothing for you to do. The check matters on the host, where the server opens that profile directly. Not during `--login` itself, which moves the old profile aside before it starts a browser and so can never trip it.

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
- `--user-data-dir PATH` - Path to persistent browser profile directory (default: ~/.linkedin-mcp/profile). Rotating or clearing a session moves and deletes this directory *and its parent*, which also holds `cookies.json`, `source-state.json` and the derived runtime profiles. A path other than the default is therefore only used once it carries a `profile-claim.json` marker, written automatically when the parent is empty or already holds a session from this server
- `--claim-profile-root` - Take over a non-default profile directory this server will not claim on its own: one whose parent already holds other files, or one carrying an ownership marker written for a different path (a mounted volume that moved). Needed once
- `--slow-mo MS` - Delay between browser actions in milliseconds (default: 0, useful for debugging)
- `--viewport WxH` - Browser viewport size (default: 1280x720). Applies to the normal windowless mode only; a headed launch (`--no-headless`, `--login`) uses the real window size
- `--chrome-path PATH` - Path to Chrome/Chromium executable (for custom browser installations)
- `--proxy-server URL` - Route the browser through a proxy, as `scheme://host:port`. Set the password via `PROXY_PASSWORD` (no flag, so it stays out of the process list)
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
- Managed browser downloads are cached at `~/.linkedin-mcp/patchright-browsers/`, shared with the `uvx` and MCP Bundle installations
- *The browser cache keeps growing*: Patchright keeps an old Chromium revision for as long as any installed version still references it, and a `uv` archive or a second worktree is such a reference. The server logs a warning naming what it holds. To reclaim the space, stop every LinkedIn MCP Server instance, delete `~/.linkedin-mcp/patchright-browsers/`, and let the next launch download the current browser.
- Use `--logout` to clear the profile and start fresh

**Python/Patchright issues:**

- Check Python version: `python --version` (should be 3.12+)
- Reinstall Patchright: `uv run patchright install chromium`
- Reinstall dependencies: `uv sync --reinstall`

**Timeout issues:**

- *Page operations failing* (elements not found, navigation hangs): increase the browser page-op timeout — `--timeout 10000` or `TIMEOUT=10000` (milliseconds, default 5000).
- *Entire tool calls timing out* (e.g. multi-section profiles, cold-start Chromium, slow containers): increase the per-tool execution timeout — `--tool-timeout 300` or `TOOL_TIMEOUT=300` (seconds, default 180).
- *First tool call with no session*: if a locally logged-in browser has a live LinkedIn session, the server auto-imports it (see `AUTO_IMPORT_FROM_BROWSER` / `--auto-import`) instead of forcing a manual login. On macOS the keychain may prompt once for Safe Storage access. If no importable browser session exists, it falls back to opening a login window and waits up to `LOGIN_INLINE_WAIT` seconds (default 25, max 45; `--login-inline-wait`) so a quick sign-in resolves in one call. If the wait elapses, the tool returns a pending signal and the model retries in about 30 seconds. Neither the auto-import nor the inline wait applies under Docker or when the server is bound to a non-loopback HTTP host. Create the session on the host with `--login`, or use the explicit Docker `--login --login-viewer` command.
- Users on slow connections may need higher values for either.

**Told to run `--login` on the host when you already did:**

- If tool calls answer "No valid LinkedIn session is available in Docker" on a machine that is *not* a container, the runtime was misdetected. This happened on Linux hosts running a Docker daemon for unrelated services. Set `LINKEDIN_MCP_CONTAINER=false` to override the detection; `true` forces the opposite.

**Using a proxy:**

> **Most people should not use one.** LinkedIn's own guidance for reducing
> security challenges is to avoid a VPN or proxy, and it scores the addresses a
> session signs in from. A home connection you have used for years is a trust
> signal; a commercial exit node with a history you cannot see is not, and
> switching to one is itself the kind of change that triggers a checkpoint.
> A proxy is worth it in one case: the server runs somewhere its address is
> obviously a data centre, or in a different country from the account's history.
> Even then, a WireGuard or Tailscale exit node on your own home network beats
> any paid provider, because the address really is yours. If you do buy one,
> take a dedicated static ISP address and keep it, rather than a rotating
> residential pool.

- Route the browser through a proxy with `--proxy-server http://host:port` (`http`, `https`, `socks4` and `socks5` are accepted). Only browser traffic is routed, not the MCP transport.
- Credentials go in `PROXY_USERNAME` and `PROXY_PASSWORD`. There is no `--proxy-password` flag on purpose: command-line arguments are readable by every other user on the machine. `PROXY_SERVER` also accepts the combined `http://user:pass@host:port` form most providers hand out.
- Chromium cannot authenticate to a SOCKS proxy, so credentials require an `http(s)` endpoint. If your provider only offers authenticated SOCKS5, run a local relay that holds the credentials and point the server at that.
- Local addresses go through the proxy too. Chromium's usual direct route for `localhost` is removed when a proxy is set, so add `PROXY_BYPASS=localhost,127.0.0.1,::1` if you need local targets reached directly.
- Auto-import is skipped while a proxy is configured: a session taken from a local browser was created on your real address, and moving it to the proxy is the very change that triggers a checkpoint. Use `--login`.
- A wrong proxy password does not report itself: Chromium retries the authentication challenge until the page times out, so it surfaces as a timeout or a failed sign-in. If sessions stop working right after you add a proxy, check the credentials before assuming the session expired.
- **Set the proxy up before creating the session.** Run `--login` with the proxy already configured. Turning a proxy on for an existing profile moves a logged-in session to a new IP, which is what triggers a LinkedIn checkpoint. The same applies to `--import-from-browser`, which imports a session created on your real IP. Use a sticky session, not a rotating pool, for the same reason.

**Custom Chrome path:**

- If Chrome is installed in a non-standard location, use `--chrome-path /path/to/chrome`
- Can also set via environment variable: `CHROME_PATH=/path/to/chrome`
- On macOS and Linux the browser must be at least as new as the one that last opened your profile, and the server refuses the launch otherwise. (Not on Windows: a browser there cannot be asked its version without starting one, so the check is off.) An older browser can silently drop stores a newer one wrote, the saved session among them, and the failure then looks exactly like an expired login. The message names both versions. Going back to the bundled Chromium after running a newer Chrome once is the usual way to meet this; either run the newer browser again, whichever one that was, or run `--login`, which moves the stored session aside and signs in fresh with the browser you have. `--logout` also clears it but discards the old session instead of keeping it recoverable, and it asks for confirmation on the terminal, so it is not usable from a server an MCP client started.
- Only Chrome, Chromium and Chrome for Testing are compared this way. Forks number themselves differently (Vivaldi is on 7.x, Edge's build number sits far below Chrome's under the same major), so pointing `CHROME_PATH` at one turns the check off rather than producing a refusal nothing could satisfy.

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
