# MCP Server for LinkedIn

A Model Context Protocol (MCP) server that connects AI assistants to LinkedIn. Access profiles, companies, and job postings through a Docker container.

> **Disclaimer:** This is an independent, community project. It is not affiliated with, authorized by, endorsed by, or sponsored by LinkedIn Corporation or Microsoft. "LinkedIn" is a registered trademark of LinkedIn Corporation and is used here only descriptively to identify the third-party service this software interoperates with.

## Features

- **Profile Access**: Get detailed LinkedIn profile information including experience, education, skills, projects, certifications, and more
- **Own Profile**: Fetch the authenticated user's own profile to give agents self-context
- **Profile Connections**: Send connection requests or accept incoming ones, with optional notes
- **Company Profiles**: Extract comprehensive company data, including the LinkedIn company URN id (used by LinkedIn's people-search `currentCompany` URL facet)
- **Company Employees**: List employees at a company with optional keyword filtering
- **Company Search**: Search for companies by keyword
- **Job Details**: Retrieve job posting information
- **Job Search**: Search for jobs with keywords and location filters
- **Saved Jobs**: List job postings saved by the authenticated user
- **People Search**: Search for people by keywords and location
- **Person Posts**: Get recent activity/posts from a person's profile
- **Company Posts**: Get recent posts from a company's LinkedIn feed
- **Home Feed**: Get recent posts from the authenticated user's LinkedIn home feed
- **Post Search**: Search posts/content globally by keyword (the "Posts" tab) with an optional recency filter
- **Compact References**: Return typed per-section links alongside readable text without shipping full-page markdown

## Quick Start

The image ships full Chromium. Log in once through a browser the container shows you in your own browser tab:

```bash
# Create the directory first so the container can save your session into it
mkdir -p ~/.linkedin-mcp
docker run -it --rm \
  -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp \
  -p 127.0.0.1:6080:6080 \
  stickerdaniel/linkedin-mcp-server:latest \
  --login --login-viewer
```

Open the full URL the command prints (it carries the access token) and sign in. The viewer closes itself afterwards; let the command exit on its own so the session is stored completely. It gives up after 30 minutes.

Keep the `-v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp` mount on every later `docker run`. A session created on the host with `uvx mcp-server-linkedin@latest --login` works too, and the container then rebuilds its own profile from those cookies on each start.

If an older rootful Docker run left that host directory owned by root, repair it with `sudo chown -R "$(id -u):$(id -g)" ~/.linkedin-mcp`.

**Configure Claude Desktop with Docker**

```json
{
  "mcpServers": {
    "mcp-server-linkedin": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-v", "/absolute/path/to/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "stickerdaniel/linkedin-mcp-server:latest"
      ]
    }
  }
}
```

Spell that first path out in full. A client runs `docker` directly rather than through a shell, so a leading `~` reaches Docker unexpanded and it refuses the mount.

> **Note:** Plain `--login` does not publish a viewer. Use `--login --login-viewer` only for the one-shot login container, with port 6080 published to loopback.
>
> **Note:** `stdio` is the default transport. Add `--transport streamable-http` only when you specifically want HTTP mode.
>
> **Note:** In HTTP mode the endpoint has no authentication, so the address it
> is published on is the only thing limiting who can use your LinkedIn session.
> Publish to loopback: `-p 127.0.0.1:8080:8080` together with
> `--host 0.0.0.0`. The wildcard host is required for the server to be reachable
> inside the container at all; the `127.0.0.1:` prefix on `-p` is what keeps it
> off your network. Without that prefix Docker publishes on every interface.
> Only expose it more widely behind something that authenticates.
>
> **Note:** Only one process uses the browser at a time; others wait briefly
> and take over when it finishes a call. That coordination does not reach
> between the host and a container sharing the mounted `~/.linkedin-mcp`
> directory, so do not run `--login` or `--logout` on the host while a
> container is running.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USER_DATA_DIR` | `~/.linkedin-mcp/profile` | Browser profile directory. The container default always works; any other path needs one run with `--claim-profile-root` first. |
| `LOG_LEVEL` | `WARNING` | Logging level: DEBUG, INFO, WARNING, ERROR |
| `TIMEOUT` | `5000` | Browser timeout in milliseconds |
| `TOOL_TIMEOUT` | `180` | Timeout for a whole tool call, in seconds. Raise it for heavy scrapes, slow networks, or a cold-start browser. |
| `LOGIN_TIMEOUT` | `1800` | How long the login browser waits for you to finish signing in, in seconds (`0` = no limit). The Docker viewer ends the login after 30 minutes either way. |
| `LOGIN_INLINE_WAIT` | `25` | How long a tool call waits for a login to finish, in seconds (max 45). Not used in Docker, where `--login --login-viewer` is the login path. |
| `BROWSER_WAIT` | `25` | How long to wait for another server process to hand over the shared browser, in seconds (max 45; `0` = report busy at once). |
| `BROWSER_MIN_HOLD` | `20` | Shortest time a process keeps the shared browser before handing it over, in seconds. Clamped to 3 seconds below `BROWSER_WAIT`, so raise that one along with it. Higher means fewer browser restarts but longer waits for other clients. |
| `BROWSER_IDLE_TIMEOUT` | `600` | Close an idle browser and release the profile after this many seconds without a tool call (`0` = keep it open). |
| `AUTO_IMPORT_FROM_BROWSER` | on | Import a session from a signed-in local browser on the first tool call that needs one. Skipped in containers, which have no host browser or keychain. |
| `TRANSPORT` | `stdio` | Transport mode: stdio, streamable-http |
| `HOST` | `127.0.0.1` | HTTP server host (for streamable-http transport) |
| `PORT` | `8000` | HTTP server port (for streamable-http transport) |
| `HTTP_PATH` | `/mcp` | HTTP server path (for streamable-http transport) |
| `SLOW_MO` | `0` | Delay between browser actions in ms (debugging) |
| `HEADLESS` | `false` | Docker runs full headed Chromium on a virtual display. Set `true` only for Chromium's real headless mode, which identifies itself as `HeadlessChrome`. |
| `DAEMON_ENABLED` | `false` | The experimental shared-browser daemon is ignored in Docker. |
| `VIEWPORT` | `1280x720` | Viewport size as WIDTHxHEIGHT. Only applies with `HEADLESS=true`; a headed Docker run uses its real window size. |
| `CHROME_PATH` | - | Path to Chrome/Chromium executable (rarely needed in Docker) |
| `PROXY_SERVER` | - | Route browser traffic through a proxy, as `scheme://host:port` (`http`, `https`, `socks4`, `socks5`), or with credentials as `http://user:pass@host:port`. Only the browser is routed, not the MCP transport. Inside a container `127.0.0.1` is the container itself, so a relay on the host is `host.docker.internal` (native Linux Docker also needs `--add-host=host.docker.internal:host-gateway`). **Most setups are better off without a proxy:** LinkedIn scores the addresses a session signs in from, so a stable known address beats a commercial exit node. |
| `PROXY_USERNAME` | - | Username for the proxy |
| `PROXY_PASSWORD` | - | Password for the proxy. Env-only, since command-line arguments are readable by every user on the machine. Chromium cannot authenticate to a SOCKS proxy, so credentials need an `http(s)` endpoint. |
| `PROXY_BYPASS` | - | Comma-separated hosts to reach directly instead of through the proxy |
| `LINKEDIN_EXPERIMENTAL_PERSIST_DERIVED_SESSION` | `false` | Experimental: keep the container's derived profile across restarts instead of rebuilding it on each start |
| `LINKEDIN_TRACE_MODE` | `on_error` | Trace retention: `on_error` keeps artifacts only from failed runs, `always` keeps every run, `off` keeps none |

**Example with custom timeouts:**

```json
{
  "mcpServers": {
    "mcp-server-linkedin": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/absolute/path/to/.linkedin-mcp:/home/pwuser/.linkedin-mcp",
        "-e", "TIMEOUT=10000",
        "-e", "TOOL_TIMEOUT=300",
        "stickerdaniel/linkedin-mcp-server"
      ]
    }
  }
}
```

## Repository

- **Source**: <https://github.com/stickerdaniel/linkedin-mcp-server>
- **License**: Apache 2.0
