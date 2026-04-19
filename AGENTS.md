# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

- Use `uv` for dependency management: `uv sync` (dev: `uv sync --group dev`)
- Lint: `uv run ruff check .` (auto-fix with `--fix`)
- Format: `uv run ruff format .`
- Type check: `uv run ty check` (using ty, not mypy)
- Tests: `uv run pytest` (with coverage: `uv run pytest --cov`)
- Pre-commit: `uv run pre-commit install` then `uv run pre-commit run --all-files`
- Run server locally: `uv run -m linkedin_mcp_server --no-headless`
- Run via uvx (PyPI/package verification only): `uvx linkedin-scraper-mcp`
- Docker build: `docker build -t linkedin-mcp-server .`
- Install browser: `uv run patchright install chromium`

## Scraping Rules

- **One section = one navigation.** Each entry in `PERSON_SECTIONS` / `COMPANY_SECTIONS` (`scraping/fields.py`) maps to exactly one page navigation. Never combine multiple URLs behind a single section.
- **Minimize DOM dependence.** Prefer innerText and URL navigation over DOM selectors. When DOM access is unavoidable, use minimal generic selectors (`a[href*="/jobs/view/"]`) — never class names tied to LinkedIn's layout.

## Tool Return Format

All scraping tools return: `{url, sections: {name: raw_text}}`.

Optional additional keys:

- `references: {section_name: [{kind, url, text?, context?}]}` — LinkedIn URLs are relative paths
- `section_errors: {section_name: {error_type, error_message, issue_template_path, runtime, ...}}`
- `unknown_sections: [name, ...]`
- `job_ids: [id, ...]` (search_jobs only)
- `posts: [{text, url?}, ...]` (get_feed only) — per-post text blocks in feed order; `url` omitted for promoted/suggested items

## Verifying Bug Reports

Always verify scraping bugs end-to-end against live LinkedIn, not just code analysis. Use `uv run`, not `uvx`, so the running process reflects your workspace. Use `uvx` only for packaged distribution verification. For live Docker investigations, refresh the source session first with `uv run -m linkedin_mcp_server --login` before testing each materially different approach. Assume a valid login profile already exists at `~/.linkedin-mcp/profile/`.

```bash
# Start server
uv run -m linkedin_mcp_server --transport streamable-http --log-level DEBUG

# Initialize MCP session (grab Mcp-Session-Id from response headers)
curl -s -D /tmp/mcp-headers -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'

# Extract the session ID from saved headers
SESSION_ID=$(grep -i 'Mcp-Session-Id' /tmp/mcp-headers | awk '{print $2}' | tr -d '\r')

# Call a tool
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

The CI release workflow automatically updates `manifest.json` and `docker-compose.yml` with the new version — do not update them manually.

After the workflow completes, file a PR in the MCP registry to update the version.

## Commit Messages

- Follow conventional commits: `type(scope): subject`
- Types: feat, fix, docs, style, refactor, test, chore, perf, ci
- Keep subject <50 chars, imperative mood

## Development Workflow

Always read [`CONTRIBUTING.md`](CONTRIBUTING.md) before filing an issue or working on this repository.

- Write a short synthetic prompt that would reproduce the PR diff if given to a fresh Claude Code session. Don't copy the user's first message — distill the conversation into a single instruction that captures the full scope of changes. This tells the maintainer what was intended, which is often more useful than reviewing the full diff. Use a Markdown blockquote under a `## Synthetic prompt` heading, followed by the model attribution:
  ```
  ## Synthetic prompt

  > Add `skills` and `projects` sections to `get_person_profile`, following the certifications PR pattern. Update fields, tests, docs, and manifest.

  Generated with <model name and version>
  ```
- When implementing a new feature/fix:
  1. Check open issues. If no issue exists, create one following the templates in `.github/ISSUE_TEMPLATE/`. Fill in every section; delete optional sections if not applicable.
  2. Branch from `main`: `feature/issue-number-short-description`
  3. Implement and test
  4. Update README.md and docs/docker-hub.md if relevant
  5. Create a draft PR; only convert to regular PR when ready to merge
  6. Review with AI agents first, then manual review. Do not squash commits.

## PR Reviews

Greptile posts initial reviews as PR review comments, but follow-ups as **issue comments**. Always check both.

```bash
gh api repos/{owner}/{repo}/pulls/{pr}/reviews    # initial reviews
gh api repos/{owner}/{repo}/pulls/{pr}/comments   # inline comments
gh api repos/{owner}/{repo}/issues/{pr}/comments   # follow-up reviews
```

## btca

When you need up-to-date information about technologies used in this project, use btca to query source repositories directly.

```bash
btca resources                           # list available resources
btca ask -r <resource> -q "<question>"
btca ask -r fastmcp -r playwright -q "How do I set up browser context with FastMCP tools?"
```
