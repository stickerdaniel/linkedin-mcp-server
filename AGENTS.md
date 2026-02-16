# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

**Environment Setup:**

- Use `uv` for dependency management: `uv sync` (installs all dependencies)
- Development dependencies: `uv sync --group dev`
- Bump version: `uv version --bump minor` (or `major`, `patch`) - this is the **only manual step** for a release. The GitHub Actions release workflow (`.github/workflows/release.yml`) automatically handles: manifest.json/docker-compose.yml version updates, git tag, Docker build & push, DXT extension, GitHub release, and PyPI publish. After the workflow completes, manually file a PR in the MCP registry to update the version.
- Install browser: `uv run patchright install chromium`
- Run server locally: `uv run -m linkedin_mcp_server --no-headless`
- Run via uvx (PyPI): `uvx linkedin-scraper-mcp`
- Run in Docker: `docker run -it --rm -v ~/.linkedin-mcp:/home/pwuser/.linkedin-mcp stickerdaniel/linkedin-mcp-server:latest`

**Code Quality:**

- Lint: `uv run ruff check .` (auto-fix with `--fix`)
- Format: `uv run ruff format .`
- Type check: `uv run ty check` (using ty, not mypy)
- Tests: `uv run pytest` (with coverage: `uv run pytest --cov`)
- Pre-commit hooks: `uv run pre-commit install` then `uv run pre-commit run --all-files`

**Docker Commands:**

- Build: `docker build -t linkedin-mcp-server .`
- Login: Use uvx locally first: `uvx linkedin-scraper-mcp --login`

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
- `scraping/` - innerText extraction engine with Flag-based section selection
- `config/` - Configuration management (schema, loaders)
- `authentication.py` - LinkedIn profile-based authentication

**Tool Categories:**

- **Person Tools** (`tools/person.py`) - Profile scraping with explicit section selection
- **Company Tools** (`tools/company.py`) - Company profile and posts extraction
- **Job Tools** (`tools/job.py`) - Job posting details and search functionality

**Available MCP Tools:**

| Tool | Description |
|------|-------------|
| `get_person_profile` | Get profile with explicit `sections` selection (experience, education, interests, honors, languages, contact_info) |
| `get_company_profile` | Get company info with explicit `sections` selection (posts, jobs) |
| `get_company_posts` | Get recent posts from company feed |
| `get_job_details` | Get job posting details |
| `search_jobs` | Search jobs by keywords and location |
| `close_session` | Close browser session and clean up resources |

**Tool Return Format:**

All scraping tools return: `{url, sections: {name: raw_text}, pages_visited, sections_requested}`

**Scraping Architecture (`scraping/`):**

- `fields.py` - `PersonScrapingFields` and `CompanyScrapingFields` Flag enums
- `extractor.py` - `LinkedInExtractor` class using navigate-scroll-innerText pattern
- **One flag = one navigation.** Each `PersonScrapingFields` / `CompanyScrapingFields` flag must map to exactly one page navigation. Never combine multiple URLs behind a single flag.

**Core Subpackage (`core/`):**

- `exceptions.py` - Exception hierarchy (AuthenticationError, RateLimitError, etc.)
- `browser.py` - `BrowserManager` with persistent context and cookie import/export
- `auth.py` - `is_logged_in()`, `wait_for_manual_login()`, `warm_up_browser()`
- `utils.py` - `detect_rate_limit()`, `scroll_to_bottom()`, `handle_modal_close()`

**Authentication Flow:**

- Uses persistent browser profile at `~/.linkedin-mcp/profile/`
- Run with `--login` to create a profile via browser login

**Transport Modes:**

- `stdio` (default) - Standard I/O for CLI MCP clients
- `streamable-http` - HTTP server mode for web-based MCP clients

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
