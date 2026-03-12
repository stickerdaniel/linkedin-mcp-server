# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Environment Setup:**

- Use `uv` for dependency management: `uv sync` (installs all dependencies)
- Development dependencies: `uv sync --group dev`
- Bump version: see [Release Process](#release-process) below
- Install browser: `uv run patchright install chromium`
- Run server locally: `uv run -m linkedin_mcp_server --no-headless`
- Run via uvx (PyPI/package verification only): `uvx linkedin-scraper-mcp`
- Run in Docker: `docker run -it --rm -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp stickerdaniel/linkedin-mcp-server:latest`

**Code Quality:**

- Lint: `uv run ruff check .` (auto-fix with `--fix`)
- Format: `uv run ruff format .`
- Type check: `uv run ty check` (using ty, not mypy)
- Tests: `uv run pytest` (with coverage: `uv run pytest --cov`)
- Pre-commit hooks: `uv run pre-commit install` then `uv run pre-commit run --all-files`

**Docker Commands:**

- Build: `docker build -t linkedin-mcp-server .`
- Login for local development: `uv run -m linkedin_mcp_server --login`
- Login for packaged-distribution verification: `uvx linkedin-scraper-mcp --login`

## Architecture Overview

This is a **LinkedIn MCP (Model Context Protocol) Server** that enables AI assistants to interact with LinkedIn through web scraping. The codebase follows a two-phase startup pattern:

1. **Authentication Phase** (`authentication.py`) - Validates LinkedIn browser profile exists
2. **Server Runtime Phase** (`server.py`) - Runs FastMCP server with tool registration

**Core Components:**

- `cli_main.py` - Entry point with CLI argument parsing and orchestration
- `server.py` - FastMCP server setup and tool registration
- `tools/` - LinkedIn scraping tools (person, company, job profiles)
- `drivers/browser.py` - Patchright browser management with persistent profile (singleton)
- `core/` - Inlined browser, auth, and utility code (replaces `linkedin_scraper` dependency)
- `scraping/` - innerText extraction engine with explicit section selection
- `config/` - Configuration management (schema, loaders)
- `authentication.py` - LinkedIn profile-based authentication

**Tool Categories:**

- **Person Tools** (`tools/person.py`) - Profile scraping with explicit section selection
- **Company Tools** (`tools/company.py`) - Company profile and posts extraction
- **Job Tools** (`tools/job.py`) - Job posting details and search functionality

**Available MCP Tools:**

| Tool | Description |
|------|-------------|
| `get_person_profile` | Get profile with explicit `sections` selection (experience, education, interests, honors, languages, contact_info, posts) |
| `get_company_profile` | Get company info with explicit `sections` selection (posts, jobs) |
| `get_company_posts` | Get recent posts from company feed |
| `get_job_details` | Get job posting details |
| `search_jobs` | Search jobs by keywords and location |
| `close_session` | Close browser session and clean up resources |
| `search_people` | Search for people by keywords and location |

**Tool Return Format:**

All scraping tools return: `{url, sections: {name: raw_text}}`.

Tools may also include:

- `references: {section_name: [{kind, url, text?, context?}, ...]}` — compact typed link targets for graph expansion. LinkedIn URLs are relative paths such as `/in/stickerdaniel/`; external URLs remain absolute.
- `section_errors: {section_name: {error_type, error_message, issue_template_path, issue_template, runtime, ...}}` when one section failed but the overall tool call still completed. These diagnostics include trace/log locations and an issue-ready markdown template.
- `unknown_sections: [name, ...]` when unknown section names were passed.
- `job_ids: [id, ...]` for `search_jobs`.

**Scraping Architecture (`scraping/`):**

- `fields.py` - `PERSON_SECTIONS` and `COMPANY_SECTIONS` config dicts mapping section name to `(url_suffix, is_overlay)`
- `extractor.py` - `LinkedInExtractor` class using navigate-scroll-innerText pattern
- **One section = one navigation.** Each entry in `PERSON_SECTIONS` / `COMPANY_SECTIONS` maps to exactly one page navigation. Never combine multiple URLs behind a single section.
- **Minimize DOM dependence.** Prefer innerText and URL navigation over DOM selectors. When DOM access is unavoidable (e.g. extracting `href` attributes, finding scrollable containers), use minimal generic selectors (`a[href*="/jobs/view/"]`) — never class names tied to LinkedIn's layout.

**Core Subpackage (`core/`):**

- `exceptions.py` - Exception hierarchy (AuthenticationError, RateLimitError, etc.)
- `browser.py` - `BrowserManager` with persistent context and cookie import/export
- `auth.py` - `is_logged_in()`, `wait_for_manual_login()`, `warm_up_browser()`
- `utils.py` - `detect_rate_limit()`, `scroll_to_bottom()`, `handle_modal_close()`

**Dependency Injection (`dependencies.py`):**

- `get_extractor()` — async factory that acquires the singleton browser, runs `ensure_authenticated()`, and returns a `LinkedInExtractor`
- Injected into tool functions via `Depends(get_extractor)` (hidden from MCP tool schema)
- No cleanup needed — browser lifecycle is managed by the server lifespan

**Authentication Flow:**

- Source runtime uses persistent browser profile at `~/.linkedin-mcp/profile/`
- `--login` creates a new source login generation and exports `cookies.json`
- Foreign runtimes derive their own persistent profiles under `~/.linkedin-mcp/runtime-profiles/<runtime-id>/profile/`
- The first foreign-runtime bridge exports `storage-state.json`, performs a checkpoint restart, and only then marks the derived runtime profile reusable
- Derived runtime profiles are reused across restarts and rebuilt only after a new host `--login`

**Transport Modes:**

- `stdio` (default) - Standard I/O for CLI MCP clients
- `streamable-http` - HTTP server mode for web-based MCP clients
- Tool calls are serialized within one server process to protect the shared
  LinkedIn browser session. Concurrent client requests queue instead of running
  in parallel. Use debug logging to inspect scraper lock wait/acquire/release.

## Development Notes

- **Python Version:** Requires Python 3.12+
- **Package Manager:** Uses `uv` for fast dependency resolution
- **Browser:** Uses Patchright (anti-detection Playwright fork) with Chromium
- **Logging:** Configurable levels, JSON format for non-interactive mode
- **Error Handling:** Comprehensive exception handling for LinkedIn rate limits, captchas, etc.

**Key Dependencies:**

- `fastmcp` - MCP server framework
- `patchright` - Anti-detection browser automation (Playwright fork)

**Configuration:**

- CLI arguments with comprehensive help (`--help`)
- Browser profile stored at `~/.linkedin-mcp/profile/`

**Commit Message Format:**

- Follow conventional commits: `type(scope): subject`
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci
- Keep subject <50 chars, imperative mood

## Commit Message Guidelines

**Commit Message Rules:**

- Always use the commit message format type(scope): subject
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci
- Keep subject <50 chars, imperative mood

## Verifying Bug Reports

Always verify scraping bugs end-to-end against live LinkedIn, not just code analysis. When working in this repository, use the local code path with `uv run`, not `uvx`, so the running process reflects the files in your workspace. Use `uvx` only when intentionally verifying the packaged distribution. For live Docker investigations, always refresh the source session first with a fresh local `uv run -m linkedin_mcp_server --login` before testing each materially different approach. Assume a valid login profile already exists at `~/.linkedin-mcp/profile/`. Start the server with HTTP transport in one terminal (this process is long-running and will block the shell), then in a second terminal call the tool via curl:

```bash
# Create or refresh the local source session
uv run -m linkedin_mcp_server --login

# Start server
uv run -m linkedin_mcp_server --transport streamable-http --log-level DEBUG

# Initialize MCP session (grab Mcp-Session-Id from response headers)
curl -s -D /tmp/mcp-headers -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Extract the session ID from saved headers
SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/mcp-headers | awk '{print $2}' | tr -d '\r')

# Call a tool (use Mcp-Session-Id from previous response)
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_person_profile","arguments":{"linkedin_username":"williamhgates","sections":"posts"}}}'
```

## Release Process

```bash
git checkout main && git pull
uv version --bump minor          # or: major, patch — updates pyproject.toml AND uv.lock
gt create -m "chore: Bump version to X.Y.Z"
gt submit                        # merge PR to trigger release workflow
```

After the workflow completes, file a PR in the MCP registry to update the version.

## Important Development Notes

### Development Workflow

- Never sign a PR or commit with Claude Code
- When implementing a new feature/fix, follow this process:
  1. Check open issues. If no issue exists for the feature, create one that follows the feature issue template.
  2. Create a new branch from `main` and name it `feature/issue-number-short-description`
  3. Implement the feature
  4. Test the feature
  5. Make sure the README.md, docs/docker-hub.md and AGENTS.md is updated with the new feature
  6. Create a PR with a short description of the feature/fix
  7. First review the PR with ai agents.
  8. Manually review the PR and merge it if it's approved. Do not squash the commits.
  9. Delete the branch after the PR is merged.

## PR Reviews

Greptile posts initial reviews as PR review comments, but follow-ups as **issue comments**. Always check both. To trigger a re-review, comment `@greptileai review` on the PR.

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews    # initial reviews
gh api repos/{owner}/{repo}/pulls/{pr}/comments   # inline comments
gh api repos/{owner}/{repo}/issues/{pr}/comments   # follow-up reviews
```

## Greptile MCP

The project includes a `.mcp.json` that configures the Greptile MCP server for Claude Code. Contributors need to set `GREPTILE_API_KEY` in their environment (get one at [app.greptile.com](https://app.greptile.com)).

For Codex CLI, run:

```bash
codex mcp add greptile --url https://api.greptile.com/mcp --bearer-token-env-var GREPTILE_API_KEY
```

## btca

When you need up-to-date information about technologies used in this project, use btca to query source repositories directly.

**Available resources**: fastmcp, patchright, pytest, ruff, ty, uv, inquirer, pythonDotenv, pyperclip, preCommit

### Usage

```bash
btca ask -r <resource> -q "<question>"
```

Use multiple `-r` flags to query multiple resources at once:

```bash
btca ask -r fastmcp -r patchright -q "How do I set up browser context with FastMCP tools?"
```
