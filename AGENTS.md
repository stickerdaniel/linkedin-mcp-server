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
- Single test: `uv run pytest tests/test_tools.py::test_name -v`
- Parallel tests: `uv run pytest -n auto` (uses pytest-xdist)
- Tests use `asyncio_mode = auto` — async test functions are collected automatically without `@pytest.mark.asyncio`
- Pre-commit hooks: `uv run pre-commit install` then `uv run pre-commit run --all-files`

**Docker Commands:**

- Build: `docker build -t linkedin-mcp-server .`
- Login: Use uvx locally first: `uvx linkedin-scraper-mcp --login`

## Architecture Overview

This is a **LinkedIn MCP (Model Context Protocol) Server** that enables AI assistants to interact with LinkedIn through web scraping. Built with FastMCP and Patchright (anti-detection Playwright fork).

### Startup Flow

Two-phase startup pattern:

1. **Authentication Phase** (`authentication.py`) - Validates LinkedIn browser profile exists at `~/.linkedin-mcp/profile/`
2. **Server Runtime Phase** (`server.py`) - Runs FastMCP server with tool registration

Entry point is `cli_main.py` which handles CLI args (`--login`, `--logout`, `--status`) before reaching phase 2. Transport modes: `stdio` (default) or `streamable-http`.

### Tool Registration Pattern

Tools are registered in `server.py` via `create_mcp_server()`:

```
register_person_tools(mcp)   → get_person_profile, search_people
register_company_tools(mcp)  → get_company_profile, get_company_posts
register_job_tools(mcp)      → get_job_details, search_jobs
register_posts_tools(mcp)    → get_my_recent_posts, get_post_comments, get_post_content, find_unreplied_comments
close_session                → registered inline
```

Each tool follows the same pattern: `ensure_authenticated()` → parse sections → `get_or_create_browser()` → `LinkedInExtractor(browser.page)` → scrape → return result.

All scraping tools return: `{url, sections: {name: raw_text}, pages_visited, sections_requested}`

### Two-Level Browser Architecture

**Level 1 — Core** (`core/browser.py`): `BrowserManager` wraps Patchright's `chromium.launch_persistent_context()`. Manages playwright, context, page instances. Handles cookie import/export for the cross-platform cookie bridge (macOS profile → Docker Linux via `cookies.json`).

**Level 2 — Driver** (`drivers/browser.py`): Module-level singleton. `get_or_create_browser()` returns existing or creates new. `close_browser()` exports cookies then cleans up. `ensure_authenticated()` uses a 120s TTL cache to avoid redundant DOM login checks.

### Scraping Engine (`scraping/`)

- **`fields.py`** — `PersonScrapingFields` and `CompanyScrapingFields` are `Flag` enums. **One flag = one page navigation.** Never combine multiple URLs behind a single flag. Section names are parsed from comma-separated strings via `parse_person_sections()` / `parse_company_sections()`.
- **`extractor.py`** — `LinkedInExtractor` implements the navigate-scroll-innerText pattern:
  1. Navigate to URL, wait for DOM load
  2. Dismiss modals (`handle_modal_close`)
  3. Scroll to load lazy content (max 5 scrolls)
  4. Extract `main.innerText` (or `body.innerText` fallback)
  5. Strip sidebar/footer noise via regex (`strip_linkedin_noise`)
  6. On soft rate limit (only chrome returned, no content): retry once after 5s backoff
  7. Humanized delay (1.5–4s) between section navigations
- **`posts.py`** — Specialized scraping for user posts, post comments, and unreplied comment detection. Uses notifications page when possible for unreplied comments, falls back to scanning recent posts.
- **`cache.py`** — `ScrapingCache` (module-level singleton, 300s TTL). Keyed by URL, skips navigation on cache hit.

### Rate Limit Handling

`RateLimitState` in `core/utils.py` uses exponential backoff: 30s → 60s → 120s → 300s cap. Success gradually decays the counter (not instant reset). Detection checks URL redirects, CAPTCHA markers, and body text heuristics via `detect_rate_limit()`.

### Configuration Flow (`config/`)

Three-layer precedence: defaults (`schema.py` dataclasses) → env vars (`load_from_env()`) → CLI args (`load_from_args()`). `AppConfig` contains `BrowserConfig` and `ServerConfig`. Accessed via `get_config()` singleton.

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `get_person_profile` | Get profile with explicit `sections` selection (experience, education, interests, honors, languages, contact_info) |
| `get_company_profile` | Get company info with explicit `sections` selection (posts, jobs) |
| `get_company_posts` | Get recent posts from company feed |
| `get_my_recent_posts` | List recent posts from the logged-in user (post_url, post_id, text_preview, created_at) |
| `get_post_comments` | Get top-level comments for a post (post_url or post_id) |
| `get_post_content` | Get the text content of a specific post (post_url or post_id) |
| `find_unreplied_comments` | Find comments on your posts without your reply (since_days, max_posts) |
| `get_notifications` | Get recent notifications from your LinkedIn notifications page (comments, reactions, connections, mentions, jobs, etc.) |
| `get_job_details` | Get job posting details |
| `search_jobs` | Search jobs by keywords and location |
| `search_people` | Search for people by keywords and location |
| `close_session` | Close browser session and clean up resources |

## Testing

Tests live in `tests/` with ~13 test modules. Key fixtures in `conftest.py` (all `autouse=True`):

- **`reset_singletons`** — Resets browser driver, config, scraping cache, and rate limit state before and after each test.
- **`isolate_profile_dir`** — Monkeypatches `DEFAULT_PROFILE_DIR` and `get_profile_dir()` across all modules to redirect to `tmp_path`. Prevents tests from touching the real `~/.linkedin-mcp/profile/`.
- **`profile_dir`** (not autouse) — Creates a fake profile directory with a placeholder `Default/Cookies` file so `profile_exists()` returns True.
- **`mock_context`** — Mock FastMCP `Context` with `AsyncMock` for `report_progress`.

When writing new tests: async functions are collected automatically (no `@pytest.mark.asyncio` needed). Use `profile_dir` fixture when the test needs an existing profile. Browser-related tests should mock at the driver level (`drivers/browser.py`) rather than the core level.

## Development Notes

- **Python Version:** Requires Python 3.12+
- **Package Manager:** Uses `uv` for fast dependency resolution
- **Browser:** Patchright (anti-detection Playwright fork) with Chromium
- **Key Dependencies:** `fastmcp` (MCP framework), `patchright` (browser automation)
- **Logging:** Configurable levels, JSON format for non-interactive mode
- **Browser profile:** Persistent at `~/.linkedin-mcp/profile/`, run `--login` to create

**Commit Message Format:**

- Follow conventional commits: `type(scope): subject`
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci
- Keep subject <50 chars, imperative mood

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
